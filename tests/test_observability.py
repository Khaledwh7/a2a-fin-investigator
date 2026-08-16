"""Phase 6 — observability: trace, latency, tokens, cost, metrics, REST API, logs."""

from __future__ import annotations

import httpx
import pytest

from app.a2a.client import A2AClient
from app.a2a.types import Message, Part
from app.agents.schemas import CustomerProfile
from app.api.factory import build_app
from app.config import Settings
from app.observability.cost import cost_usd, estimate_tokens
from app.observability.logging import redact
from app.observability.metrics import Metrics


# --------------------------------------------------------------------------- #
# Cost / token unit tests
# --------------------------------------------------------------------------- #
def test_cost_and_token_estimate():
    # 1M input @ $15 + 1M output @ $75 = $90 for claude-opus-5.
    assert cost_usd("claude-opus-5", 1_000_000, 1_000_000) == pytest.approx(90.0)
    assert cost_usd("unknown-model", 1000, 1000) == 0.0
    assert estimate_tokens("a" * 400) == 100     # ~4 chars/token


def test_metrics_snapshot():
    m = Metrics()
    m.inc("investigations_total")
    m.inc("agent_calls_total", 5)
    m.observe("investigation_latency_ms", 12.0)
    m.observe("investigation_latency_ms", 20.0)
    snap = m.snapshot()
    assert snap["counters"]["investigations_total"] == 1
    assert snap["counters"]["agent_calls_total"] == 5
    assert snap["latency"]["investigation_latency_ms"]["count"] == 2
    assert snap["latency"]["investigation_latency_ms"]["avg_ms"] == 16.0


# --------------------------------------------------------------------------- #
# Log redaction (data-leakage protection)
# --------------------------------------------------------------------------- #
def test_redaction_masks_secrets():
    out = redact({"authorization": "Bearer abc.def.ghi", "api_key": "sk-123",
                  "note": "call used Bearer zzz.yyy", "ok": "visible"})
    assert out["authorization"] == "***"
    assert out["api_key"] == "***"
    assert "zzz.yyy" not in out["note"]     # bearer scrubbed inside free text
    assert out["ok"] == "visible"


# --------------------------------------------------------------------------- #
# Trace captured during a real investigation
# --------------------------------------------------------------------------- #
@pytest.fixture
def app_client():
    settings = Settings(require_human_review=False)
    app = build_app(settings)
    transport = httpx.ASGITransport(app=app)
    app.state.orchestrator.set_client(A2AClient(transport=transport, max_attempts=1))
    return settings, app, transport


async def test_trace_has_per_agent_spans_and_latency(app_client):
    settings, app, transport = app_client
    user = A2AClient(transport=transport, max_attempts=1)
    msg = Message(parts=[Part.from_data({"profile": CustomerProfile.demo().model_dump()})])
    task = await user.send_message(settings.orchestrator_url, msg)

    trace = app.state.trace_store.get(task.context_id)
    assert trace is not None
    assert trace.agent_calls == 6              # KYC/AML/Sanctions/Fraud/Risk/Reporting
    assert trace.errors == 0
    assert trace.wall_ms > 0
    # Each A2A hop is a span with a real (non-negative) latency.
    agents = [s.agent for s in trace.spans if s.kind == "a2a_call"]
    assert agents == ["kyc", "aml", "sanctions", "fraud", "risk", "reporting"]
    assert all(s.duration_ms >= 0 for s in trace.spans)
    assert trace.est_tokens > 0                         # heuristic token accounting
    await user.aclose()


async def test_metrics_increment_after_run(app_client):
    settings, app, transport = app_client
    user = A2AClient(transport=transport, max_attempts=1)
    msg = Message(parts=[Part.from_data({"profile": CustomerProfile.demo().model_dump()})])
    await user.send_message(settings.orchestrator_url, msg)

    snap = app.state.metrics.snapshot()
    assert snap["counters"]["investigations_total"] == 1
    assert snap["counters"]["agent_calls_total"] == 6
    assert snap["latency"]["investigation_latency_ms"]["count"] == 1
    await user.aclose()


# --------------------------------------------------------------------------- #
# Human REST API (what the Streamlit UI calls)
# --------------------------------------------------------------------------- #
async def test_rest_api_investigation_flow(app_client):
    settings, app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as http:
        health = await http.get("/healthz")
        assert health.json() == {"status": "ok"}

        resp = await http.post("/investigations",
                               json={"profile": CustomerProfile.demo().model_dump()})
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["risk_band"] == "CRITICAL"
        assert body["summary"]["sar_recommended"] is True
        assert body["summary"]["latency_ms"] > 0
        assert body["summary"]["agent_calls"] == 6
        assert body["trace"]["est_tokens"] > 0

        # Metrics endpoint reflects the run.
        metrics = (await http.get("/metrics")).json()
        assert metrics["counters"]["investigations_total"] == 1

        # List + get single investigation.
        listed = (await http.get("/investigations")).json()
        assert len(listed) >= 1
        task_id = body["task"]["id"]
        one = (await http.get(f"/investigations/{task_id}")).json()
        assert one["task"]["id"] == task_id
        assert one["trace"]["agent_calls"] == 6
        # Detail carries the same rich summary as a fresh run (Case-History "Open").
        assert one["summary"]["decision"]
        assert one["summary"]["customer"] == "Viktor Petrov"


async def test_audit_endpoint_reports_chain_and_events(app_client):
    _settings, _app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as http:
        await http.post("/investigations",
                        json={"profile": CustomerProfile.demo().model_dump()})
        audit = (await http.get("/audit")).json()
        assert audit["chain_valid"] is True          # tamper-evident chain holds
        assert audit["count"] >= 2
        actions = {e["action"] for e in audit["entries"]}
        assert {"investigation_started", "investigation_completed"} <= actions
        assert all("hash" in e for e in audit["entries"])


async def test_rest_api_validation_rejects_bad_profile(app_client):
    settings, app, transport = app_client
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as http:
        # Empty full_name violates the CustomerProfile schema → 422.
        resp = await http.post("/investigations",
                               json={"profile": {"full_name": ""}})
    assert resp.status_code == 422
