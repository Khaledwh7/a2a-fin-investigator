"""The Streamlit console, driven headlessly.

The UI is where a wrong answer actually reaches a person, so these tests care
most about the things that would mislead one: a degraded run must not render a
verdict, analyst-supplied text must not reach the page as markup, and the
override control must not be able to silently downgrade a case.

``AppTest`` runs the real script in-process. The REST layer is stubbed, so what
is under test is the UI's own logic rather than the API's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from ui import api as ui_api
from ui import intake, samples
from ui.state import VIEW_ABOUT, VIEW_AUDIT, VIEW_HISTORY, VIEW_METRICS, VIEW_QUEUE

# Absolute, so the suite is independent of both the working directory and of
# Streamlit's resolution rule for relative paths — which changed in 1.62 from
# "relative to the CWD" to "relative to the file calling AppTest.from_file()".
APP = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")
HEALTH_OK = {"status": "ok", "security": {"agent_auth": True, "signed_cards": True,
                                          "rate_limit": False, "human_review": True}}


# --------------------------------------------------------------------------- #
# Fixtures — a fake REST layer
# --------------------------------------------------------------------------- #
def _task(*, task_id: str = "task-1", customer: str = "Viktor Petrov",
          state: str = "TASK_STATE_COMPLETED", score: int = 97,
          band: str = "CRITICAL") -> dict[str, Any]:
    """A wire-format task carrying the artifacts the UI reads."""
    return {
        "id": task_id,
        "contextId": "ctx-1",
        "status": {"state": state},
        "history": [{"parts": [{"data": {"profile": {
            "full_name": customer, "country": "Russia", "occupation": "consultant",
        }}}]}],
        "artifacts": [
            {"name": "kyc_findings", "parts": [{"data": {
                "identity_verified": True, "data_quality_score": 90,
                "is_pep": False, "risk_flags": [], "checks": []}}]},
            {"name": "aml_findings", "parts": [{"data": {
                "ledger_available": True, "transaction_count": 9,
                "total_volume": 88000, "near_threshold_count": 4,
                "flagged_transactions": [], "suspicious_patterns": ["structuring"],
                "transactions": []}}]},
            {"name": "sanctions_findings", "parts": [{"data": {
                "match_tier": "STRONG", "blocked_country_exposure": False,
                "matches": [], "counterparty_matches": []}}]},
            {"name": "fraud_findings", "parts": [{"data": {
                "assessed": True, "fraud_score": 40, "fraud_band": "MEDIUM",
                "fraud_alert": False, "typologies": []}}]},
            {"name": "risk_assessment", "parts": [{"data": {
                "risk_score": score, "risk_band": band,
                "dimensions": {}, "score_breakdown": [],
                "escalations": ["confirmed sanctions match → CRITICAL floor"],
                "confidence": {"value": 90, "label": "high", "basis": "full coverage"},
                "coverage_pct": 100}}]},
            {"name": "investigation_report",
             "parts": [{"text": "# Investigation report\n\nFull detail."}]},
        ],
    }


def _result(**kw: Any) -> dict[str, Any]:
    task = _task(**kw)
    risk = task["artifacts"][4]["parts"][0]["data"]
    return {
        "task": task,
        "trace": {"spans": [{"kind": "a2a_call", "agent": a, "status": "ok",
                             "duration_ms": 12.0}
                            for a in ("kyc", "aml", "sanctions", "fraud", "risk",
                                      "reporting")]},
        "summary": {"state": task["status"]["state"], "risk_band": risk["risk_band"],
                    "risk_score": risk["risk_score"], "latency_ms": 880,
                    "agent_calls": 6, "est_tokens": 5000,
                    "recommended_actions": ["File a SAR"], "pending_review": False},
    }


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub every REST call the UI makes; tests override individual entries."""
    calls: dict[str, Any] = {"decisions": [], "deleted": [], "cleared": 0}

    monkeypatch.setattr(ui_api, "health", lambda *_a, **_k: True)
    monkeypatch.setattr(ui_api, "health_detail", lambda *_a, **_k: dict(HEALTH_OK))
    monkeypatch.setattr(ui_api, "list_investigations", lambda *_a, **_k: [])
    monkeypatch.setattr(ui_api, "get_metrics", lambda *_a, **_k: {"counters": {}})
    monkeypatch.setattr(ui_api, "get_audit", lambda *_a, **_k: {})

    def submit_decision(_url, task_id, action, note="", band=None, analyst=None):
        calls["decisions"].append({"task_id": task_id, "action": action,
                                   "note": note, "band": band, "analyst": analyst})
        return _result()

    def delete_investigation(_url, task_id, analyst=None):
        calls["deleted"].append(task_id)
        return {"deleted": 6}

    monkeypatch.setattr(ui_api, "submit_decision", submit_decision)
    monkeypatch.setattr(ui_api, "delete_investigation", delete_investigation)
    return calls


def _run(**session: Any) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30)
    for key, value in session.items():
        at.session_state[key] = value
    return at.run()


def _page_text(at: AppTest) -> str:
    """Everything the page rendered, as one searchable string."""
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [e.value for e in at.error] + [i.value for i in at.info]
    parts += [s.value for s in at.success] + [w.value for w in at.warning]
    return "\n".join(str(p) for p in parts)


# --------------------------------------------------------------------------- #
# Boot & navigation
# --------------------------------------------------------------------------- #
def test_app_boots_without_exception(fake_api):
    at = _run()
    assert not at.exception
    assert "AI Financial Investigation Assistant" in _page_text(at)


def test_app_boots_when_api_is_unreachable(monkeypatch):
    """The console must load and say so, not crash, when the API is down."""
    monkeypatch.setattr(ui_api, "health", lambda *_a, **_k: False)
    monkeypatch.setattr(ui_api, "health_detail", lambda *_a, **_k: {})
    monkeypatch.setattr(ui_api, "list_investigations",
                        lambda *_a, **_k: (_ for _ in ()).throw(ui_api.ApiError("down")))
    at = _run()
    assert not at.exception
    assert "API offline" in _page_text(at)


@pytest.mark.parametrize("view", [VIEW_QUEUE, VIEW_HISTORY, VIEW_METRICS,
                                  VIEW_AUDIT, VIEW_ABOUT])
def test_every_view_renders(fake_api, view):
    """Each nav destination renders on its own, with no data available."""
    at = _run(nav=view)
    assert not at.exception


def test_default_view_is_intake(fake_api):
    at = _run()
    assert "New investigation" in _page_text(at)
    assert at.session_state["nav"] == "🔎 Investigate"


# --------------------------------------------------------------------------- #
# Intake form
# --------------------------------------------------------------------------- #
def test_intake_renders_every_section(fake_api):
    at = _run()
    text = _page_text(at)
    for section in ["1 · Identity", "2 · Employment & wealth",
                    "3 · Account & onboarding", "4 · Identity document",
                    "5 · Observed activity & behaviour", "Transaction ledger"]:
        assert section in text, f"missing form section: {section}"


def test_run_detection_requires_a_name(fake_api):
    """Submitting an empty form must explain itself, not call the API."""
    at = _run()
    at.button(key="FormSubmitter:intake_form-🔍 Run detection").click().run()
    assert not at.exception
    assert any("Full legal name is required" in e.value for e in at.error)


def test_loading_a_sample_fills_the_form_and_its_ledger():
    state: dict[str, Any] = {}
    intake.load_sample("Viktor Petrov — sanctioned + structuring", state)
    assert state["f_full_name"] == "Viktor Petrov"
    assert state["f_country"] == "Russia"
    assert state["f_behaviours"]
    # The ledger comes with it — a sample without transactions would leave AML
    # and Fraud unassessed and misrepresent the case.
    assert len(state["ledger_rows"]) == 9


def test_every_sample_case_carries_a_ledger():
    """A demo case with no transactions would show 'not assessed' and teach the
    reader the wrong thing about the system."""
    for name in samples.SAMPLE_CASES:
        state: dict[str, Any] = {}
        intake.load_sample(name, state)
        assert state["ledger_rows"], f"{name} has no ledger"
        assert intake.clean_ledger(state["ledger_rows"]), f"{name} ledger is unusable"


# --------------------------------------------------------------------------- #
# Ledger normalisation (the NaN class of bug)
# --------------------------------------------------------------------------- #
def test_clean_ledger_drops_rows_without_an_amount():
    rows = intake.clean_ledger([
        {"date": "2026-01-01", "amount": 100.0, "direction": "in"},
        {"date": "2026-01-02", "amount": 0, "direction": "in"},
        {"date": "2026-01-03", "amount": None, "direction": "in"},
    ])
    assert len(rows) == 1


def test_clean_ledger_coerces_nan_cells_to_empty_strings():
    """A half-filled grid row arrives with NaN; sent as-is it is invalid JSON."""
    nan = float("nan")
    rows = intake.clean_ledger([{"date": nan, "amount": 500.0, "direction": nan,
                                 "counterparty": nan, "channel": nan, "country": nan}])
    assert rows == [{"date": "", "amount": 500.0, "direction": "in",
                     "counterparty": "", "channel": "wire", "country": ""}]


def test_clean_ledger_rejects_out_of_vocabulary_values():
    rows = intake.clean_ledger([{"amount": 10.0, "direction": "sideways",
                                 "channel": "carrier pigeon"}])
    assert rows[0]["direction"] == "in"
    assert rows[0]["channel"] == "wire"


def test_profile_from_form_translates_behaviours_into_engine_keywords():
    state = dict(intake.FORM_DEFAULTS)
    state["f_full_name"] = "  Jane Doe  "
    state["f_behaviours"] = ["Frequent cash deposits just under reporting thresholds"]
    state["f_notes"] = "seen at branch"
    profile = intake.profile_from_form(state)
    assert profile["full_name"] == "Jane Doe"          # trimmed
    assert "structuring" in profile["notes"]           # behaviour → keyword
    assert "seen at branch" in profile["notes"]        # free text preserved


def test_profile_from_form_blanks_the_not_listed_sentinels():
    state = dict(intake.FORM_DEFAULTS)
    state["f_country"] = "Other (not listed)"
    state["f_industry"] = "Not provided"
    state["f_sow"] = "Not provided"
    profile = intake.profile_from_form(state)
    assert profile["country"] == ""
    assert profile["industry"] == ""
    assert profile["source_of_wealth"] == ""


# --------------------------------------------------------------------------- #
# The live streaming run
# --------------------------------------------------------------------------- #
def _stream(*frames: dict[str, Any]):
    """A stubbed SSE stream; records the profile the UI actually submitted."""
    sent: dict[str, Any] = {}

    def stream_investigation(_url, profile, analyst=None):
        sent["profile"] = profile
        sent["analyst"] = analyst
        yield from frames

    return stream_investigation, sent


def _named(at: AppTest, name: str = "Jane Doe") -> AppTest:
    at.session_state["f_full_name"] = name
    return at


def test_a_successful_run_stores_the_result(fake_api, monkeypatch):
    stream, sent = _stream(
        {"type": "status", "meta": {"agent": "kyc"}, "note": "screening identity"},
        {"type": "artifact", "name": "kyc_findings", "summary": "identity verified"},
        {"type": "done", **_result()})
    monkeypatch.setattr(ui_api, "stream_investigation", stream)

    at = _named(_run())
    at.button(key="FormSubmitter:intake_form-🔍 Run detection").click().run()
    assert not at.exception
    assert sent["profile"]["full_name"] == "Jane Doe"
    assert at.session_state["last_result"]["summary"]["risk_band"] == "CRITICAL"


def test_the_submitted_profile_carries_the_ledger(fake_api, monkeypatch):
    """The ledger is the only transaction evidence — it must reach the API."""
    stream, sent = _stream({"type": "done", **_result()})
    monkeypatch.setattr(ui_api, "stream_investigation", stream)

    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["ledger_rows"] = samples.clean_salary_ledger()
    at.run()
    _named(at)
    at.button(key="FormSubmitter:intake_form-🔍 Run detection").click().run()
    assert len(sent["profile"]["transactions"]) == 5


def test_the_analyst_is_attributed_on_the_run(fake_api, monkeypatch):
    stream, sent = _stream({"type": "done", **_result()})
    monkeypatch.setattr(ui_api, "stream_investigation", stream)

    at = _named(_run(analyst="Dana Reed"))
    at.button(key="FormSubmitter:intake_form-🔍 Run detection").click().run()
    assert sent["analyst"] == "Dana Reed"


def test_an_error_frame_surfaces_and_stores_no_result(fake_api, monkeypatch):
    stream, _sent = _stream({"type": "error", "detail": "sanctions agent unreachable"})
    monkeypatch.setattr(ui_api, "stream_investigation", stream)

    at = _named(_run())
    at.button(key="FormSubmitter:intake_form-🔍 Run detection").click().run()
    assert not at.exception
    assert any("sanctions agent unreachable" in e.value for e in at.error)
    assert "last_result" not in at.session_state


def test_a_truncated_stream_is_reported_rather_than_silently_ignored(fake_api,
                                                                     monkeypatch):
    """No 'done' frame means no verdict — the UI must say so, not sit blank."""
    stream, _sent = _stream({"type": "status", "meta": {"agent": "kyc"}})
    monkeypatch.setattr(ui_api, "stream_investigation", stream)

    at = _named(_run())
    at.button(key="FormSubmitter:intake_form-🔍 Run detection").click().run()
    assert any("ended before returning a result" in e.value for e in at.error)
    assert "last_result" not in at.session_state


def test_an_api_failure_mid_stream_is_caught(fake_api, monkeypatch):
    def exploding(_url, _profile, analyst=None):
        raise ui_api.ApiError("connection reset")
        yield  # pragma: no cover — generator marker

    monkeypatch.setattr(ui_api, "stream_investigation", exploding)
    at = _named(_run())
    at.button(key="FormSubmitter:intake_form-🔍 Run detection").click().run()
    assert not at.exception
    assert any("connection reset" in e.value for e in at.error)


def test_run_is_refused_when_the_api_is_down(fake_api, monkeypatch):
    monkeypatch.setattr(ui_api, "health", lambda *_a, **_k: False)
    at = _named(_run())
    at.button(key="FormSubmitter:intake_form-🔍 Run detection").click().run()
    assert any("not reachable" in e.value for e in at.error)


# --------------------------------------------------------------------------- #
# Results — the verdict, and when there must not be one
# --------------------------------------------------------------------------- #
def test_completed_case_renders_its_verdict_and_report(fake_api):
    at = _run(last_result=_result())
    assert not at.exception
    text = _page_text(at)
    assert "Risk assessment" in text
    assert "Investigation report" in text
    assert "Full detail." in text


def test_degraded_run_shows_no_verdict(fake_api):
    """The one failure mode a compliance tool must never have: a partial
    pipeline presented as a clean rating."""
    result = _result(state="TASK_STATE_FAILED")
    result["trace"]["spans"][2] = {"kind": "a2a_call", "agent": "sanctions",
                                   "status": "error", "duration_ms": 7000,
                                   "error": "peer unreachable"}
    at = _run(last_result=result)
    assert not at.exception
    text = _page_text(at)
    assert "incomplete" in text.lower()
    # The score, radar and report are suppressed for an incomplete run.
    assert "Risk assessment" not in text
    assert "Investigation report" not in text
    # Findings that DID arrive stay visible for diagnosis.
    assert "Partial findings" in text


def test_analyst_supplied_name_is_escaped_not_rendered_as_markup(fake_api):
    """Customer names are analyst input and reach an HTML sink."""
    payload = "<img src=x onerror=alert(1)>"
    at = _run(last_result=_result(customer=payload))
    assert not at.exception
    text = _page_text(at)
    assert "<img src=x" not in text
    assert "&lt;img" in text


# --------------------------------------------------------------------------- #
# Human-in-the-loop review
# --------------------------------------------------------------------------- #
def _pending_result(band: str = "CRITICAL") -> dict[str, Any]:
    result = _result(state="TASK_STATE_INPUT_REQUIRED", band=band)
    result["summary"]["pending_review"] = True
    return result


def test_review_panel_renders_for_a_paused_case(fake_api):
    at = _run(last_result=_pending_result())
    assert not at.exception
    text = _page_text(at)
    assert "Awaiting" in text and "review" in text
    assert "Your decision" in text


def test_override_is_disabled_until_a_different_band_is_chosen(fake_api):
    """An override that defaults to the model's own band cannot fire by accident.

    The old panel put the band selector *below* the buttons and defaulted it to
    LOW, so one stray click on a CRITICAL case silently downgraded it.
    """
    at = _run(last_result=_pending_result(band="CRITICAL"))
    override = next(b for b in at.button if "Override to" in b.label)
    assert override.label == "✏️ Override to CRITICAL"
    assert override.disabled, "override must be inert while it would change nothing"


def test_override_targets_the_band_the_analyst_picked(fake_api):
    at = _run(last_result=_pending_result(band="CRITICAL"))
    at.selectbox(key="override_band").select("MEDIUM").run()
    override = next(b for b in at.button if "Override to" in b.label)
    assert not override.disabled
    assert override.label == "✏️ Override to MEDIUM"
    override.click().run()
    assert fake_api["decisions"] == [
        {"task_id": "task-1", "action": "override", "note": "", "band": "MEDIUM",
         "analyst": None}]


def test_approve_files_the_case_at_its_assigned_band(fake_api):
    at = _run(last_result=_pending_result(band="HIGH"))
    approve = next(b for b in at.button if "Approve & file" in b.label)
    assert approve.label == "✅ Approve & file (HIGH)"
    approve.click().run()
    assert fake_api["decisions"][0]["action"] == "approve"
    assert fake_api["decisions"][0]["band"] is None


def test_decision_records_the_analyst_and_rationale(fake_api):
    at = _run(last_result=_pending_result(), analyst="  Dana Reed  ")
    at.text_input(key="decision_note").input("verified with source docs").run()
    next(b for b in at.button if "Close" in b.label).click().run()
    recorded = fake_api["decisions"][0]
    assert recorded["action"] == "close"
    assert recorded["note"] == "verified with source docs"
    assert recorded["analyst"] == "Dana Reed"      # trimmed for the audit log


# --------------------------------------------------------------------------- #
# Review queue & history
# --------------------------------------------------------------------------- #
QUEUE_ROW = {"task_id": "task-1", "customer": "Viktor Petrov",
             "risk_band": "CRITICAL", "risk_score": 97,
             "state": "TASK_STATE_INPUT_REQUIRED"}


def test_empty_queue_says_so_rather_than_rendering_nothing(fake_api):
    at = _run(nav=VIEW_QUEUE)
    assert "Nothing awaiting review" in _page_text(at)


def test_queue_lists_pending_cases_only(fake_api, monkeypatch):
    monkeypatch.setattr(ui_api, "list_investigations", lambda *_a, **_k: [
        QUEUE_ROW,
        {"task_id": "task-2", "customer": "John Smith", "risk_band": "LOW",
         "risk_score": 3, "state": "TASK_STATE_COMPLETED"},
    ])
    at = _run(nav=VIEW_QUEUE)
    text = _page_text(at)
    assert "Viktor Petrov" in text
    assert "John Smith" not in text          # completed cases are not queue work


def test_queue_delete_is_behind_a_confirmation(fake_api, monkeypatch):
    monkeypatch.setattr(ui_api, "list_investigations", lambda *_a, **_k: [QUEUE_ROW])
    at = _run(nav=VIEW_QUEUE)
    # The destructive control is never a bare button in the row.
    assert not any("Delete" in b.label for b in at.button if b.key is None)
    confirm = at.button(key="danger_q_task-1")
    assert "Delete permanently" in confirm.label
    confirm.click().run()
    assert fake_api["deleted"] == ["task-1"]


def test_history_shows_a_table_and_asks_for_a_selection(fake_api, monkeypatch):
    monkeypatch.setattr(ui_api, "list_investigations", lambda *_a, **_k: [QUEUE_ROW])
    at = _run(nav=VIEW_HISTORY)
    assert not at.exception
    assert len(at.dataframe) == 1
    assert "Select a row" in _page_text(at)


def test_sidebar_badges_the_pending_count(fake_api, monkeypatch):
    monkeypatch.setattr(ui_api, "list_investigations", lambda *_a, **_k: [QUEUE_ROW])
    at = _run()
    assert "1 awaiting review" in _page_text(at)


# --------------------------------------------------------------------------- #
# Audit log & security posture
# --------------------------------------------------------------------------- #
AUDIT = {"chain_valid": True, "count": 2, "entries": [
    {"seq": 1, "ts": "2026-08-19T10:00:00", "actor": "user:dana",
     "action": "investigation_started", "resource": "Viktor Petrov",
     "outcome": "ok", "hash": "abc123def456"},
    {"seq": 2, "ts": "2026-08-19T10:00:01", "actor": "agent:orchestrator",
     "action": "invoke", "resource": "kyc", "outcome": "allowed",
     "hash": "def456abc123"},
]}


def test_audit_log_reports_a_verified_chain(fake_api, monkeypatch):
    monkeypatch.setattr(ui_api, "get_audit", lambda *_a, **_k: AUDIT)
    at = _run(nav=VIEW_AUDIT)
    assert not at.exception
    assert "chain verified" in _page_text(at)
    assert len(at.dataframe) == 1


def test_audit_log_reports_a_broken_chain(fake_api, monkeypatch):
    broken = dict(AUDIT, chain_valid=False)
    monkeypatch.setattr(ui_api, "get_audit", lambda *_a, **_k: broken)
    at = _run(nav=VIEW_AUDIT)
    assert "chain broken" in _page_text(at)


def test_audit_has_no_per_entry_delete(fake_api, monkeypatch):
    """Deleting one row and re-hashing the rest is the forgery the chain exists
    to expose, so the only removal offered is the whole log."""
    monkeypatch.setattr(ui_api, "get_audit", lambda *_a, **_k: AUDIT)
    at = _run(nav=VIEW_AUDIT)
    delete_buttons = [b for b in at.button if "Delete" in b.label]
    assert len(delete_buttons) == 1
    assert "Delete all 2 entries" in delete_buttons[0].label


def test_about_page_reads_posture_from_the_api_not_from_claims(fake_api):
    """Rate limiting is off in this configuration and the page must say so."""
    at = _run(nav=VIEW_ABOUT)
    text = _page_text(at)
    assert "✅ JWT auth + RBAC" in text
    assert "⚪ Rate limiting" in text
    assert "off in this configuration" in text


def test_about_page_marks_disabled_security_when_the_api_is_open(fake_api, monkeypatch):
    monkeypatch.setattr(ui_api, "health_detail", lambda *_a, **_k: {
        "status": "ok", "security": {"agent_auth": False, "signed_cards": False,
                                     "rate_limit": False, "human_review": False}})
    at = _run(nav=VIEW_ABOUT)
    text = _page_text(at)
    assert "⚪ JWT auth + RBAC" in text
    assert "A2A open" in text


def test_sidebar_shows_the_secured_badge_when_the_api_is_locked_down(fake_api):
    at = _run()
    assert "A2A secured" in _page_text(at)
