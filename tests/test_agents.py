"""Phase 3 — specialist logic, tool RBAC, and a full orchestrated investigation.

The integration test assembles all six agents into one app and drives a real
A2A investigation end to end (the orchestrator makes in-process HTTP calls to
the specialists via an ASGI-transport client).
"""

from __future__ import annotations

import httpx
import pytest

from app.a2a.client import A2AClient
from app.a2a.types import Message, Part, TaskState
from app.agents.schemas import CustomerProfile
from app.api.factory import build_app
from app.config import AgentRole, Settings
from app.tools.finance import build_default_registry
from app.tools.registry import ToolAccessDenied


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
    app.state.orchestrator.set_client(A2AClient(transport=transport, max_attempts=1))
    user = A2AClient(transport=transport, max_attempts=1)
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
