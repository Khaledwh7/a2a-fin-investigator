"""Specialist logic, tool RBAC, and a full orchestrated investigation.

The integration test assembles all six agents into one app and drives a real
A2A investigation end to end (the orchestrator makes in-process HTTP calls to
the specialists via an ASGI-transport client).
"""

from __future__ import annotations

import httpx
import pytest

from app.a2a.client import A2AClient
from app.a2a.types import Message, Part, TaskState
from app.agents.schemas import CustomerProfile, Transaction
from app.api.factory import build_app
from app.config import AgentRole, Settings
from app.tools.finance import build_default_registry
from app.tools.registry import ToolAccessDenied
from tests.conftest import user_client, wire_orchestrator


# --------------------------------------------------------------------------- #
# Tool registry — least privilege
# --------------------------------------------------------------------------- #
def test_registry_enforces_least_privilege():
    reg = build_default_registry()
    # KYC may verify identity...
    out = reg.call("verify_identity", "kyc", profile={"full_name": "A", "id_document": {}})
    assert "identity_verified" in out
    # ...but the Risk agent may NOT screen sanctions.
    with pytest.raises(ToolAccessDenied):
        reg.call("screen_sanctions", "risk", full_name="A", country="X")


def test_sanctions_tool_matches_known_entry():
    reg = build_default_registry()
    hit = reg.call("screen_sanctions", "sanctions", full_name="Viktor Petrov",
                   country="Russia")
    assert hit["hit"] is True
    assert hit["matches"][0]["matched_name"] == "Viktor Petrov"


# --------------------------------------------------------------------------- #
# Evidence integrity — the scores must come from real input, never invented data
# --------------------------------------------------------------------------- #
def test_no_ledger_never_fabricates_transactions():
    """With no ledger there is no transaction analysis — and no invented rows."""
    reg = build_default_registry()
    profile = {"full_name": "Nora Bishop", "country": "Ireland",
               "notes": "cash deposits just under threshold structuring; rapid layering"}

    aml = reg.call("analyze_transactions", "aml", profile=profile)
    assert aml["ledger_available"] is False
    assert aml["transactions"] == [] and aml["flagged_transactions"] == []
    assert aml["transaction_count"] == 0 and aml["total_volume"] == 0.0
    # Notes that name a typology must NOT become verified findings.
    assert aml["structuring_detected"] is False
    assert aml["suspicious_patterns"] == []
    assert aml["sar_candidate"] is False
    assert aml["unassessed_reason"]

    # They are carried as attested observations instead, explicitly unverified.
    observed = {o["indicator"]: o for o in aml["attested_observations"]}
    assert {"structuring", "rapid_movement"} <= set(observed)
    assert all(o["verified"] is False for o in observed.values())

    fraud = reg.call("detect_fraud", "fraud", profile=profile)
    assert fraud["assessed"] is False
    assert fraud["typologies"] == [] and fraud["fraud_alert"] is False


def test_same_profile_twice_gives_identical_numbers():
    """No randomness anywhere: identical input ⇒ byte-identical findings."""
    reg = build_default_registry()
    profile = CustomerProfile.demo().model_dump()
    assert (reg.call("analyze_transactions", "aml", profile=profile)
            == reg.call("analyze_transactions", "aml", profile=profile))


def test_unassessed_dimensions_are_excluded_not_scored_zero():
    """A dimension with no evidence is dropped from the blend, not counted clean."""
    from app.agents.risk import RiskExecutor

    risk = RiskExecutor(build_default_registry())
    profile = CustomerProfile(full_name="Nora Bishop", country="Panama")
    aml = {"ledger_available": False, "attested_observations": [],
           "unassessed_reason": "no ledger"}
    out = risk.analyze(profile, {"kyc": {"data_quality_score": 80}, "aml": aml,
                                 "sanctions": {}, "fraud": {"assessed": False}})

    assert out["unassessed_dimensions"] == ["transaction", "fraud"]
    assert out["dimensions"]["transaction"]["assessed"] is False
    assert out["dimensions"]["fraud"]["reason"]
    # 1 - 0.22 - 0.20 = 0.58 of the model could be assessed.
    assert out["coverage_pct"] == 58
    # Geographic risk must not be diluted by the two dimensions we could not score:
    # Panama (70) at its renormalised weight alone exceeds its raw-weight share.
    assert out["risk_score"] > round(0.18 * 70)
    # An unassessed model can never claim high confidence.
    assert out["confidence"]["value"] <= 60
    assert out["confidence"]["unassessed"] == ["transaction", "fraud"]


# --------------------------------------------------------------------------- #
# Scoring integrity — the score must reflect the evidence, not just a threshold
# --------------------------------------------------------------------------- #
def _score(**kw):
    """Run the risk model over a synthetic finding set."""
    from app.agents.risk import RiskExecutor

    risk = RiskExecutor(build_default_registry())
    profile = kw.pop("profile", CustomerProfile(full_name="Test Subject"))
    ctx = {"kyc": {}, "aml": {"ledger_available": True, "transaction_count": 12},
           "sanctions": {}, "fraud": {"assessed": True, "fraud_score": 0}}
    for key, value in kw.items():
        ctx[key] = {**ctx.get(key, {}), **value}
    return risk.analyze(profile, ctx)


def test_escalation_floor_keeps_the_band_but_not_a_flat_score():
    """A floor guarantees the band; it must not throw away the other evidence.

    Two sanctioned customers — one otherwise clean, one with everything else
    wrong — used to score an identical 90, leaving an analyst no way to triage.
    """
    hit = {"match_tier": "STRONG", "matches": [{"matched_name": "X", "program": "P",
                                                "match_score": 100}]}
    clean_but_sanctioned = _score(sanctions=hit)
    also_everything_else = _score(
        profile=CustomerProfile(full_name="Test Subject", country="Iran"),
        sanctions={**hit, "blocked_country_exposure": True},
        aml={"ledger_available": True, "transaction_count": 12,
             "structuring_detected": True, "rapid_movement": True,
             "near_threshold_count": 5, "passthrough_counterparties": ["x"],
             "cash_ratio": 0.9, "cash_ratio_basis": "inflow"},
        fraud={"assessed": True, "fraud_score": 90, "typologies": []},
        kyc={"is_pep": True, "data_quality_score": 100})

    # Both must stay CRITICAL — the floor's guarantee is preserved...
    assert clean_but_sanctioned["risk_band"] == "CRITICAL"
    assert also_everything_else["risk_band"] == "CRITICAL"
    # ...but they must be distinguishable within it.
    assert also_everything_else["risk_score"] > clean_but_sanctioned["risk_score"]
    assert clean_but_sanctioned["risk_score"] >= 90        # floor still respected


def test_score_never_lands_flat_on_the_floor_value():
    """The floor is a minimum, not the answer: evidence above it still counts."""
    low = _score(kyc={"is_pep": True})                      # PEP floor = 30
    high = _score(kyc={"is_pep": True, "data_quality_score": 0,
                       "missing_fields": ["a", "b"], "industry_risk": 90,
                       "remote_onboarding": True, "income_mismatch": True},
                  profile=CustomerProfile(full_name="Test Subject", country="Panama"))
    assert low["risk_band"] == high["risk_band"] == "MEDIUM"
    assert high["risk_score"] > low["risk_score"]


def _cash_ledger(count: int, amount: float) -> list[dict]:
    return [{"date": f"2026-01-{d + 1:02d}", "amount": amount, "direction": "in",
             "counterparty": "Cash Deposit", "channel": "cash"} for d in range(count)]


def test_score_scales_with_the_magnitude_of_what_was_found():
    """Detectors must respond to how badly they fired, not just that they did.

    Ten deposits at 99% of the reporting threshold is a materially worse case
    than three at 86%, and the score has to be able to say so.
    """
    reg = build_default_registry()
    from app.agents.risk import RiskExecutor

    def dim(count: int, amount: float) -> int:
        aml = reg.call("analyze_transactions", "aml",
                       profile={"full_name": "T", "transactions": _cash_ledger(count, amount)})
        return RiskExecutor._transaction_dim(aml)["score"]

    # More deposits ⇒ higher, at a fixed amount.
    by_count = [dim(n, 9000.0) for n in (3, 5, 7, 10)]
    assert by_count == sorted(by_count) and by_count[-1] > by_count[0]
    # Tighter to the threshold ⇒ higher, at a fixed count.
    by_proximity = [dim(5, a) for a in (8600.0, 9000.0, 9500.0, 9950.0)]
    assert by_proximity == sorted(by_proximity) and by_proximity[-1] > by_proximity[0]
    # And the two axes together span a meaningful range, not two buckets.
    assert len({dim(n, a) for n in (3, 5, 7, 10)
                for a in (8600.0, 9000.0, 9500.0, 9950.0)}) >= 8


def test_indicators_accumulate_without_saturating_at_100():
    """A plain sum pins every bad case at 100 and loses the ordering above it."""
    from app.agents.risk import _combine

    assert _combine([]) == 0
    assert _combine([30]) == 30
    steps = [_combine([30] * n) for n in range(1, 8)]
    assert steps == sorted(steps)          # monotonic
    assert steps[-1] < 100                 # never saturates
    assert _combine([90, 80, 70]) < 100


def test_sar_recommendation_cannot_coexist_with_a_low_band():
    """A report reading 'LOW risk' beside 'SAR recommended' contradicts itself."""
    from app.agents.risk import RiskExecutor

    risk = RiskExecutor(build_default_registry())
    reg = build_default_registry()
    # Textbook structuring, nothing else wrong: low-risk country, clean KYC.
    profile = CustomerProfile(full_name="Plain Person", country="Ireland",
                              date_of_birth="1985-01-01",
                              id_document={"type": "passport", "number": "IE1"},
                              transactions=_cash_ledger(9, 9900.0))
    aml = reg.call("analyze_transactions", "aml", profile=profile.model_dump())
    assert aml["sar_candidate"] is True

    out = risk.analyze(profile, {"kyc": {"data_quality_score": 100}, "aml": aml,
                                 "sanctions": {},
                                 "fraud": {"assessed": True, "fraud_score": 0}})
    assert out["sar_recommended"] is True
    assert out["risk_band"] != "LOW"
    assert out["risk_score"] >= 25


def test_cash_intensity_cannot_be_diluted_by_unrelated_outflow():
    """Cash is measured against the side it moves on.

    Dividing by gross throughput let a customer mask a pile of cash deposits by
    adding ordinary outbound payments.
    """
    reg = build_default_registry()
    cash_in = [{"date": f"2026-01-0{d}", "amount": 9500.0, "direction": "in",
                "counterparty": "Cash Deposit", "channel": "cash"} for d in (1, 2, 3, 4)]
    # Enough ordinary outbound payments to drag a gross-throughput ratio under
    # the 30% trigger (38,000 cash vs 90,000 out ⇒ 0.297 gross).
    padding = [{"date": f"2026-01-{10 + d:02d}", "amount": 9000.0, "direction": "out",
                "counterparty": f"Supplier {d}", "channel": "transfer"}
               for d in range(1, 11)]

    plain = reg.call("analyze_transactions", "aml",
                     profile={"full_name": "C", "transactions": cash_in})
    padded = reg.call("analyze_transactions", "aml",
                      profile={"full_name": "C", "transactions": cash_in + padding})

    assert plain["cash_ratio_basis"] == padded["cash_ratio_basis"] == "inflow"
    assert padded["cash_ratio"] == plain["cash_ratio"] == 1.0
    # The old gross-throughput formula would have fallen under the 30% trigger.
    gross = padded["cash_total"] / (padded["total_in"] + padded["total_out"])
    assert gross < 0.3 < padded["cash_ratio"]
    assert any("cash-intensive" in p for p in padded["suspicious_patterns"])


def test_confidence_is_capped_by_how_much_evidence_exists():
    """Thin evidence cannot yield a confident rating, however complete the KYC."""
    from app.agents.risk import RiskExecutor

    full = {"a": {"assessed": True}}
    perfect_kyc = {"data_quality_score": 100}

    plenty = RiskExecutor._confidence(
        perfect_kyc, {"transaction_count": 20, "ledger_available": True}, {}, full, 1.0)
    thin = RiskExecutor._confidence(
        perfect_kyc, {"transaction_count": 1, "ledger_available": True}, {}, full, 1.0)
    none = RiskExecutor._confidence(
        perfect_kyc, {"transaction_count": 0, "ledger_available": False}, {}, full, 1.0)

    assert plenty["label"] == "HIGH"
    assert thin["label"] == "LOW"          # a single row is not a pattern
    assert none["label"] == "LOW"
    assert none["value"] < thin["value"] < plenty["value"]
    assert "capped" in thin["basis"] and "capped" in none["basis"]


# --------------------------------------------------------------------------- #
# Full investigation (client → orchestrator → 5 agents → report)
# --------------------------------------------------------------------------- #
@pytest.fixture
def app_and_client():
    # These tests exercise the automated pipeline; the human-review gate is
    # covered by tests/test_human_in_the_loop.py.
    settings = Settings(require_human_review=False)
    app = build_app(settings)
    transport = httpx.ASGITransport(app=app)
    # The orchestrator's *internal* client and the *user* client both talk to
    # the same app in-process.
    wire_orchestrator(app, transport)
    user = user_client(app, transport)
    return settings, app, user


async def _investigate(user: A2AClient, settings: Settings, profile: CustomerProfile):
    msg = Message(parts=[Part.from_data({"profile": profile.model_dump()})])
    return await user.send_message(settings.orchestrator_url, msg)


async def test_full_investigation_high_risk(app_and_client):
    settings, _app, user = app_and_client
    task = await _investigate(user, settings, CustomerProfile.demo())

    assert task.status.state == TaskState.COMPLETED

    # The orchestrator's task carries every specialist's artifact + the report.
    names = [a.name for a in task.artifacts]
    assert names == ["kyc_findings", "aml_findings", "sanctions_findings",
                     "fraud_findings", "risk_assessment", "investigation_report"]

    risk = next(a for a in task.artifacts if a.name == "risk_assessment").first_data()
    assert risk["risk_band"] == "CRITICAL"
    assert risk["sar_recommended"] is True

    sanctions = next(a for a in task.artifacts
                     if a.name == "sanctions_findings").first_data()
    assert sanctions["hit"] is True

    report = next(a for a in task.artifacts if a.name == "investigation_report")
    report_text = report.parts[0].text
    assert "Investigation Report - Viktor Petrov" in report_text

    # The report is the filed deliverable: every section must be present, and the
    # evidence sections must show the evidence, not just assert a conclusion.
    headings = [ln for ln in report_text.splitlines() if ln.startswith("## ")]
    assert len(headings) == 11, headings
    for keyword in ("Executive Summary", "Recommended Actions", "Subject Profile",
                    "KYC", "AML", "Fraud", "Sanctions", "Risk Assessment",
                    "Evidence", "Analyst Decision", "Methodology"):
        assert any(keyword.lower() in h.lower() for h in headings), keyword

    # Traceable detail, not just totals.
    assert "| txn_" in report_text                      # the flagged rows themselves
    assert "OFAC SDN" in report_text                    # the matched list entry
    assert "Jaro–Winkler" in report_text                # how the match was made
    assert "sanctions 25%" in report_text               # the weights behind the score
    assert "CRITICAL ≥ 75" in report_text               # the band thresholds
    # Section 8 must reconcile: contributions sum to the pre-escalation blend.
    assert "renormalised" in report_text
    assert "confirmed sanctions match → CRITICAL floor" in report_text
    await user.aclose()


async def test_full_investigation_low_risk(app_and_client):
    settings, _app, user = app_and_client
    clean = CustomerProfile(
        full_name="John Smith", country="United Kingdom", occupation="teacher",
        date_of_birth="1985-06-01",
        id_document={"type": "passport", "number": "GB1234567"},
        notes="Regular salary deposits.",
    )
    task = await _investigate(user, settings, clean)
    assert task.status.state == TaskState.COMPLETED

    risk = next(a for a in task.artifacts if a.name == "risk_assessment").first_data()
    assert risk["risk_band"] in {"LOW", "MEDIUM"}
    assert risk["risk_score"] < 50

    sanctions = next(a for a in task.artifacts
                     if a.name == "sanctions_findings").first_data()
    assert sanctions["hit"] is False
    await user.aclose()


async def test_customer_details_drive_the_score(app_and_client):
    """The exact KYC fields (industry, onboarding, declared PEP, income) move risk."""
    settings, _app, user = app_and_client
    # A clean-country, non-sanctioned person whose *profile details* are the risk:
    # crypto industry + remote onboarding + self-declared PEP + income mismatch.
    subject = CustomerProfile(
        full_name="Nadia Brandt", country="Germany", nationality="Germany",
        date_of_birth="1988-02-02", occupation="trader",
        industry="Cryptocurrency / digital assets", onboarding_channel="remote",
        pep_declared=True, annual_income=20_000, expected_monthly_volume=30_000,
        declared_source_of_funds="Trading",
        id_document={"type": "passport", "number": "DE99"})
    task = await _investigate(user, settings, subject)

    kyc = next(a for a in task.artifacts if a.name == "kyc_findings").first_data()
    assert kyc["industry_risk"] >= 70
    assert kyc["remote_onboarding"] is True
    assert kyc["income_mismatch"] is True

    risk = next(a for a in task.artifacts if a.name == "risk_assessment").first_data()
    cust = risk["dimensions"]["customer"]
    factor_keys = {f["factor"] for f in cust["factors"]}
    assert {"industry_risk", "non_face_to_face", "income_mismatch",
            "declared_pep"} <= factor_keys
    assert cust["score"] >= 70                       # customer dimension is elevated
    assert risk["risk_band"] in {"MEDIUM", "HIGH", "CRITICAL"}  # not LOW
    await user.aclose()


async def test_counterparty_sanctions_and_velocity(app_and_client):
    """Beneficiary screening + date-based velocity both drive the outcome."""
    settings, _app, user = app_and_client
    # A clean, non-sanctioned customer — the risk comes from WHO they pay and a
    # burst of activity in one week.
    ledger = [{"date": f"2026-02-0{i+1}", "amount": 2000.0 + i * 500,
               "direction": "out", "counterparty": f"Supplier {i}", "channel": "wire",
               "country": ""} for i in range(7)]
    ledger.append({"date": "2026-02-06", "amount": 8000.0, "direction": "out",
                   "counterparty": "Global Horizon Shipping LLC",  # on the sanctions list
                   "channel": "wire", "country": "Iran"})
    subject = CustomerProfile(
        full_name="Clarissa Webb", country="Germany", nationality="Germany",
        date_of_birth="1986-05-05", id_document={"type": "passport", "number": "DE7"},
        declared_source_of_funds="Business income",
        transactions=[Transaction(**t) for t in ledger])
    task = await _investigate(user, settings, subject)

    sanc = next(a for a in task.artifacts if a.name == "sanctions_findings").first_data()
    assert sanc["counterparty_hit"] is True
    assert sanc["counterparty_matches"][0]["counterparty"] == "Global Horizon Shipping LLC"

    aml = next(a for a in task.artifacts if a.name == "aml_findings").first_data()
    assert aml["velocity_max_7d"] >= 6            # date-based velocity window
    assert aml["high_velocity"] is True

    fraud = next(a for a in task.artifacts if a.name == "fraud_findings").first_data()
    assert any(t["type"] == "velocity_spike" for t in fraud["typologies"])

    risk = next(a for a in task.artifacts if a.name == "risk_assessment").first_data()
    assert risk["risk_band"] == "CRITICAL"        # paying a sanctioned party
    assert any("beneficiary" in e for e in risk["escalations"])
    await user.aclose()


async def test_enriched_findings_carry_analyst_detail(app_and_client):
    """Findings expose transaction-level detail, tiers, confidence & actions."""
    settings, _app, user = app_and_client
    task = await _investigate(user, settings, CustomerProfile.demo())

    aml = next(a for a in task.artifacts if a.name == "aml_findings").first_data()
    assert aml["transactions"], "full transaction list must be present"
    assert aml["flagged_transactions"], "flagged subset must be present"
    assert all("flags" in t for t in aml["transactions"])
    assert aml["near_threshold_count"] >= 3          # demo is a structurer

    sanc = next(a for a in task.artifacts if a.name == "sanctions_findings").first_data()
    assert sanc["match_tier"] == "STRONG"
    assert sanc["recommended_action"]

    kyc = next(a for a in task.artifacts if a.name == "kyc_findings").first_data()
    assert isinstance(kyc["checks"], list) and kyc["checks"]

    risk = next(a for a in task.artifacts if a.name == "risk_assessment").first_data()
    assert risk["decision"] == "DECLINE & FILE SAR"
    assert risk["confidence"]["label"] in {"LOW", "MEDIUM", "HIGH"}
    assert risk["recommended_actions"]
    # Multi-dimensional model: four dimensions each contribute to the waterfall,
    # and escalation floors can only raise the final score above the blend.
    assert set(risk["dimensions"]) == {"sanctions", "transaction", "fraud",
                                       "geographic", "customer"}
    assert risk["escalations"], "a confirmed sanctions hit must record an escalation"
    assert risk["score_breakdown"][-1]["running_total"] <= risk["risk_score"]
    await user.aclose()


async def test_investigation_fails_loudly_when_specialist_unreachable(app_and_client):
    """A broken pipeline must not masquerade as a clean, low-risk result."""
    settings, app, user = app_and_client
    # Point the sanctions peer at a path that doesn't exist → its A2A calls fail.
    app.state.orchestrator.peers[AgentRole.SANCTIONS] = \
        "http://localhost:8000/a2a/does-not-exist"

    task = await _investigate(user, settings, CustomerProfile.demo())
    assert task.status.state == TaskState.FAILED
    assert "sanctions" in (task.status.message.text if task.status.message else "")
    await user.aclose()


async def test_local_peer_not_listening_falls_back_in_process():
    """A single-process deploy must survive a wrong peer PORT.

    Every role is hosted by this one app, so a local peer URL that refuses
    connections is a config fault, not an outage: the call is served in-process
    (same JSON-RPC → auth → executor path) and the investigation still completes,
    with the failover recorded for the operator.
    """
    closed = {f"{role.value}_url": f"http://localhost:9/a2a/{role.value}"
              for role in AgentRole}          # port 9 = discard, nothing listens
    settings = Settings(require_human_review=False, retry_max_attempts=1,
                        peer_connect_timeout_seconds=2.0, **closed)
    app = build_app(settings)
    transport = httpx.ASGITransport(app=app)
    user = user_client(app, transport)

    task = await _investigate(user, settings, CustomerProfile.demo())

    assert task.status.state == TaskState.COMPLETED
    assert [a.name for a in task.artifacts] == [
        "kyc_findings", "aml_findings", "sanctions_findings", "fraud_findings",
        "risk_assessment", "investigation_report"]
    # The fallback is reported, never silent.
    assert app.state.a2a_client.loopback_origins == ["http://localhost:9"]
    await user.aclose()
    await app.state.orchestrator.aclose()


async def test_unreachable_remote_peer_still_degrades_loudly(app_and_client):
    """The in-process fallback must NOT mask a genuinely remote peer being down."""
    settings, app, user = app_and_client
    remote = A2AClient(max_attempts=1, connect_timeout=2.0)
    remote.set_loopback(httpx.ASGITransport(app=app))
    app.state.orchestrator.set_client(remote)
    app.state.orchestrator.peers[AgentRole.KYC] = \
        "http://kyc.invalid.example:8000/a2a/kyc"

    task = await _investigate(user, settings, CustomerProfile.demo())
    assert task.status.state == TaskState.FAILED
    assert "kyc" in (task.status.message.text if task.status.message else "")
    # The remote origin is never served locally, whatever the other peers do.
    assert "http://kyc.invalid.example:8000" not in remote.loopback_origins
    await user.aclose()
    await remote.aclose()


async def test_context_id_shared_across_agents(app_and_client):
    """The A2A 'Context' concept: one contextId threads the whole investigation."""
    settings, app, user = app_and_client
    task = await _investigate(user, settings, CustomerProfile.demo())

    # Every specialist created its Task under the orchestrator's contextId.
    kyc_store = app.state.agents  # role → A2AAgent
    kyc_tasks = await kyc_store[AgentRole.KYC].tasks.list(context_id=task.context_id)
    assert kyc_tasks, "KYC agent should have a task under the shared contextId"
    assert kyc_tasks[0].context_id == task.context_id
    await user.aclose()
