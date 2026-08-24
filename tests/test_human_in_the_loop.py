"""Human-in-the-loop — the A2A INPUT_REQUIRED pause/resume for high-stakes cases.

With ``require_human_review`` on, a HIGH/CRITICAL / SAR / sanctions-hit outcome
pauses the task at ``TASK_STATE_INPUT_REQUIRED`` (no report filed yet) and waits
for an analyst to **approve**, **override** or **close** it — then resumes the
same task/context to finalise. Low-risk cases still auto-complete.
"""

from __future__ import annotations

import httpx
import pytest

from app.agents.schemas import CustomerProfile
from app.api.factory import build_app
from app.config import Settings
from tests.conftest import wire_orchestrator


@pytest.fixture
def hitl():
    settings = Settings(require_human_review=True)
    app = build_app(settings)
    transport = httpx.ASGITransport(app=app)
    wire_orchestrator(app, transport)
    return app, transport


async def _post(transport, path: str, body: dict) -> dict:
    async with httpx.AsyncClient(transport=transport, base_url="http://hitl") as c:
        resp = await c.post(path, json=body)
    return {"status": resp.status_code, "json": resp.json() if resp.content else {}}


def _names(task: dict) -> list[str]:
    return [a["name"] for a in task["artifacts"]]


async def test_high_risk_pauses_for_review(hitl):
    app, transport = hitl
    r = await _post(transport, "/investigations",
                    {"profile": CustomerProfile.demo().model_dump()})
    s = r["json"]["summary"]
    assert s["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert s["pending_review"] is True
    # Findings exist, but the report is NOT filed until a human decides.
    names = _names(r["json"]["task"])
    assert "risk_assessment" in names
    assert "investigation_report" not in names
    entries = await app.state.audit.entries()
    assert any(e.action == "review_requested" for e in entries)


async def test_low_risk_auto_completes(hitl):
    _app, transport = hitl
    clean = CustomerProfile(full_name="John Smith", country="United Kingdom",
                            date_of_birth="1985-06-01",
                            declared_source_of_funds="Salary / employment",
                            id_document={"type": "passport", "number": "GB1234567"})
    r = await _post(transport, "/investigations", {"profile": clean.model_dump()})
    assert r["json"]["summary"]["state"] == "TASK_STATE_COMPLETED"
    assert r["json"]["summary"]["pending_review"] is False


async def test_analyst_approve_finalises(hitl):
    app, transport = hitl
    r = await _post(transport, "/investigations",
                    {"profile": CustomerProfile.demo().model_dump()})
    task_id = r["json"]["task"]["id"]

    r2 = await _post(transport, f"/investigations/{task_id}/decision",
                     {"action": "approve", "note": "confirmed by MLRO"})
    s = r2["json"]["summary"]
    assert s["state"] == "TASK_STATE_COMPLETED"
    assert s["decision"] == "DECLINE & FILE SAR"          # recommendation upheld
    assert "investigation_report" in _names(r2["json"]["task"])
    # Full 6-agent timeline preserved across the pause.
    assert r2["json"]["trace"]["agent_calls"] == 6

    entries = await app.state.audit.entries()
    assert any(e.action == "human_decision" for e in entries)
    assert await app.state.audit.verify_chain() is True


async def test_analyst_override_downgrades(hitl):
    _app, transport = hitl
    r = await _post(transport, "/investigations",
                    {"profile": CustomerProfile.demo().model_dump()})
    task_id = r["json"]["task"]["id"]

    r2 = await _post(transport, f"/investigations/{task_id}/decision",
                     {"action": "override", "override_band": "LOW",
                      "note": "verified legitimate"})
    s = r2["json"]["summary"]
    assert s["state"] == "TASK_STATE_COMPLETED"
    assert s["risk_band"] == "LOW"
    assert s["decision"] == "APPROVE"                     # analyst had the last word


async def test_analyst_close_dismisses(hitl):
    _app, transport = hitl
    r = await _post(transport, "/investigations",
                    {"profile": CustomerProfile.demo().model_dump()})
    task_id = r["json"]["task"]["id"]

    r2 = await _post(transport, f"/investigations/{task_id}/decision",
                     {"action": "close", "note": "false positive"})
    s = r2["json"]["summary"]
    assert s["state"] == "TASK_STATE_COMPLETED"
    assert s["decision"].startswith("NO ACTION")
    assert s["sar_recommended"] is False


async def test_decision_on_non_paused_task_is_409(hitl):
    _app, transport = hitl
    clean = CustomerProfile(full_name="John Smith", country="United Kingdom",
                            date_of_birth="1985-06-01",
                            id_document={"type": "passport", "number": "GB1234567"})
    r = await _post(transport, "/investigations", {"profile": clean.model_dump()})
    task_id = r["json"]["task"]["id"]  # already COMPLETED (low risk)
    r2 = await _post(transport, f"/investigations/{task_id}/decision",
                     {"action": "approve"})
    assert r2["status"] == 409
