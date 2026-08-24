"""Human-facing REST API — the friendly front door for the UI.

This is an *implementation choice*, not A2A. The Streamlit UI doesn't speak
JSON-RPC; it POSTs a customer profile here, and this gateway drives the
investigation through the Orchestrator (in-process) and returns the task, the
execution trace (per-agent latency + cost) and a summary. In production this
route would sit behind the analyst's SSO; it is the trusted human boundary.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.a2a.types import Message, Part, Task, TaskState
from app.agents.schemas import CustomerProfile
from app.config import AgentRole


class InvestigationRequest(BaseModel):
    profile: CustomerProfile


class DecisionRequest(BaseModel):
    action: str = "approve"                 # approve | override | close
    note: str = ""
    override_band: str | None = None        # CRITICAL | HIGH | MEDIUM | LOW


class InvestigationResponse(BaseModel):
    task: dict[str, Any]
    trace: dict[str, Any] | None
    summary: dict[str, Any]


def _artifact_data(task: Task, name: str) -> dict[str, Any]:
    art = next((a for a in task.artifacts if a.name == name), None)
    return (art.first_data() if art else {}) or {}


def _customer_from_history(task: Task) -> str | None:
    for msg in task.history:
        data = msg.first_data()
        if isinstance(data, dict) and isinstance(data.get("profile"), dict):
            return data["profile"].get("full_name")
    return None


def _summary(task: Task, trace: dict[str, Any] | None) -> dict[str, Any]:
    # Final report if present; otherwise the risk assessment (paused mid-review).
    report = _artifact_data(task, "investigation_report")
    risk = _artifact_data(task, "risk_assessment")
    conf = risk.get("confidence", {}) or {}
    pending = task.status.state == TaskState.INPUT_REQUIRED
    return {
        "state": task.status.state.value,
        "pending_review": pending,
        "customer": report.get("customer") or _customer_from_history(task),
        "risk_score": report.get("risk_score", risk.get("risk_score")),
        "risk_band": report.get("risk_band", risk.get("risk_band")),
        "decision": report.get("decision", risk.get("decision")),
        "confidence": report.get("confidence", conf.get("value")),
        "confidence_label": report.get("confidence_label", conf.get("label")),
        "sar_recommended": report.get("sar_recommended",
                                      risk.get("sar_recommended")),
        "recommendation": report.get("recommendation", risk.get("recommendation")),
        "recommended_actions": report.get("recommended_actions",
                                          risk.get("recommended_actions", [])),
        "human_review": report.get("human_review"),
        "latency_ms": (trace or {}).get("wall_ms"),
        "agent_calls": (trace or {}).get("agent_calls"),
        "est_tokens": (trace or {}).get("est_tokens"),
        "llm_tokens": (trace or {}).get("llm_tokens"),
        "cost_usd": (trace or {}).get("cost_usd"),
    }


# Who is driving the console. Sent by the UI as a header so every route — run,
# decide, delete — attributes its audit entry to a person rather than to a
# hardcoded placeholder. Without an identity the log answers "what happened" but
# never "who did it", which is most of the reason to keep one.
AnalystHeader = Header(default=None, alias="X-Analyst")


def _caller(analyst: str | None) -> str:
    """Normalise the header into an audit actor."""
    name = (analyst or "").strip()[:60]
    return f"user:{name}" if name else "user:unidentified"


def _sse(payload: dict[str, Any]) -> str:
    """One SSE frame; the blank line is the event terminator."""
    return f"data: {json.dumps(payload)}\n\n"


def _status_text(status: dict[str, Any]) -> str:
    message = status.get("message") or {}
    return " ".join(p["text"] for p in message.get("parts", []) if p.get("text"))


def _artifact_text(artifact: dict[str, Any]) -> str:
    return " ".join(p["text"] for p in artifact.get("parts", []) if p.get("text"))


def register_human_api(app: FastAPI) -> None:
    """Mount the human REST routes; reads dependencies off ``app.state``."""
    router = APIRouter(tags=["investigations"])

    @router.get("/healthz")
    async def healthz() -> dict[str, Any]:  # noqa: ANN202
        """Liveness + a2a wiring. ``degraded_peers`` is non-empty when a peer URL
        was not listening and is being served in-process — the deployment still
        works, but the ``*_URL`` settings do not match reality."""
        client = getattr(app.state, "a2a_client", None)
        degraded = client.loopback_origins if client is not None else []
        settings = app.state.settings
        return {
            "status": "ok",
            "a2a_transport": "loopback" if degraded else "http",
            "degraded_peers": degraded,
            # Reported so the UI can state the posture it is actually running
            # under, rather than advertising controls that happen to be off.
            "security": {
                "agent_auth": settings.require_agent_auth,
                "signed_cards": settings.require_signed_agent_cards,
                "rate_limit": settings.rate_limit_enabled,
                "human_review": settings.require_human_review,
            },
        }

    @router.get("/metrics")
    async def metrics() -> dict[str, Any]:  # noqa: ANN202
        return app.state.metrics.snapshot()

    @router.post("/investigations", response_model=InvestigationResponse)
    async def create_investigation(req: InvestigationRequest,
                                   analyst: str | None = AnalystHeader
                                   ) -> InvestigationResponse:
        agent = app.state.agents[AgentRole.ORCHESTRATOR]
        message = Message(parts=[Part.from_data({"profile": req.profile.model_dump()})])
        # Drive the Orchestrator directly (this gateway is the trusted caller).
        result = await agent.send_message({"message": message.to_wire()},
                                          caller=_caller(analyst))
        task = Task.model_validate(result)
        trace_obj = app.state.trace_store.get(task.context_id)
        trace = trace_obj.to_dict() if trace_obj else None
        return InvestigationResponse(task=result, trace=trace,
                                     summary=_summary(task, trace))

    @router.post("/investigations/stream")
    async def create_investigation_stream(req: InvestigationRequest,
                                          analyst: str | None = AnalystHeader
                                          ) -> StreamingResponse:
        """The same investigation, streamed as it happens.

        This is the A2A ``SendStreamingMessage`` event stream translated into
        plain SSE for the browser: the UI sees each agent start and each findings
        Artifact land as the orchestrator emits them, rather than watching a
        spinner and being told afterwards what happened.
        """
        agent = app.state.agents[AgentRole.ORCHESTRATOR]
        message = Message(parts=[Part.from_data({"profile": req.profile.model_dump()})])

        async def events() -> AsyncIterator[str]:
            task_id = context_id = ""
            async for frame in agent.stream_message({"message": message.to_wire()},
                                                    caller=_caller(analyst)):
                if "task" in frame:
                    task_id = frame["task"].get("id", "")
                    context_id = frame["task"].get("contextId", "")
                    yield _sse({"type": "accepted", "task_id": task_id,
                                "context_id": context_id})
                elif "statusUpdate" in frame:
                    update = frame["statusUpdate"]
                    status = update.get("status", {}) or {}
                    yield _sse({"type": "status",
                                "state": status.get("state"),
                                "note": _status_text(status),
                                "meta": update.get("metadata") or {}})
                elif "artifactUpdate" in frame:
                    artifact = frame["artifactUpdate"].get("artifact", {}) or {}
                    yield _sse({"type": "artifact", "name": artifact.get("name"),
                                "summary": _artifact_text(artifact)})

            # Final frame carries exactly what the blocking endpoint returns, so
            # the UI renders a streamed run and a replayed one the same way.
            task = await agent.tasks.get(task_id)
            if task is None:
                yield _sse({"type": "error", "detail": "investigation not found"})
                return
            trace_obj = app.state.trace_store.get(task.context_id)
            trace = trace_obj.to_dict() if trace_obj else None
            yield _sse({"type": "done", "task": task.to_wire(), "trace": trace,
                        "summary": _summary(task, trace)})

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @router.post("/investigations/{task_id}/decision",
                 response_model=InvestigationResponse)
    async def submit_decision(task_id: str, req: DecisionRequest,
                              analyst: str | None = AnalystHeader
                              ) -> InvestigationResponse:
        """Human-in-the-loop: an analyst approves / overrides / closes a paused case."""
        agent = app.state.agents[AgentRole.ORCHESTRATOR]
        task = await agent.tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="investigation not found")
        if task.status.state != TaskState.INPUT_REQUIRED:
            raise HTTPException(status_code=409,
                                detail="investigation is not awaiting review")
        # Resume the SAME task/context with the analyst's decision (A2A continuation).
        message = Message(task_id=task_id, context_id=task.context_id,
                          parts=[Part.from_data({"human_decision": req.model_dump()})])
        result = await agent.send_message({"message": message.to_wire()},
                                          caller=_caller(analyst))
        final = Task.model_validate(result)
        trace_obj = app.state.trace_store.get(final.context_id)
        trace = trace_obj.to_dict() if trace_obj else None
        return InvestigationResponse(task=result, trace=trace,
                                     summary=_summary(final, trace))

    @router.get("/investigations")
    async def list_investigations(limit: int = 50) -> list[dict[str, Any]]:  # noqa: ANN202
        repo = app.state.investigations
        if repo is not None:
            return [vars(s) for s in await repo.list(limit)]
        # In-memory fallback (no DB): summarize the orchestrator's own tasks.
        store = app.state.agents[AgentRole.ORCHESTRATOR].tasks
        tasks = await store.list()
        return [{"task_id": t.id, "context_id": t.context_id,
                 "state": t.status.state.value} for t in tasks][-limit:]

    @router.get("/investigations/{task_id}")
    async def get_investigation(task_id: str) -> dict[str, Any]:  # noqa: ANN202
        store = app.state.agents[AgentRole.ORCHESTRATOR].tasks
        task = await store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="investigation not found")
        trace_obj = app.state.trace_store.get(task.context_id)
        trace = trace_obj.to_dict() if trace_obj else None
        return {"task": task.to_wire(), "trace": trace,
                "summary": _summary(task, trace)}

    @router.delete("/investigations/{task_id}")
    async def delete_investigation(task_id: str,
                                   analyst: str | None = AnalystHeader
                                   ) -> dict[str, Any]:  # noqa: ANN202
        """Remove one investigation and every agent task in its context.

        The specialists' tasks share the orchestrator's ``contextId``, so the
        whole case is deleted as a unit rather than leaving orphaned agent tasks
        behind. The deletion is itself recorded in the audit log — removing a
        case must not remove the evidence that it existed.
        """
        store = app.state.agents[AgentRole.ORCHESTRATOR].tasks
        task = await store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="investigation not found")

        customer = _customer_from_history(task) or task_id
        deleted = 0
        for agent in app.state.agents.values():
            deleted += await agent.tasks.delete_context(task.context_id)

        await app.state.audit.record(
            actor=_caller(analyst), action="investigation_deleted", resource=customer,
            outcome="deleted",
            detail={"task_id": task_id, "context_id": task.context_id,
                    "tasks_removed": deleted})
        return {"deleted": deleted, "task_id": task_id,
                "context_id": task.context_id, "customer": customer}

    @router.delete("/audit")
    async def clear_audit(analyst: str | None = AnalystHeader
                          ) -> dict[str, Any]:  # noqa: ANN202
        """Clear the audit log and open a fresh chain.

        Individual entries are deliberately not deletable: editing one and
        re-hashing the rest is the forgery ``verify_chain`` exists to catch, so
        the only honest removal is all of it. The clearance is written as the
        first entry of the new chain.
        """
        cleared = await app.state.audit.clear()
        await app.state.audit.record(
            actor=_caller(analyst), action="audit_log_cleared", resource="audit_log",
            outcome="ok", detail={"entries_removed": cleared})
        return {"cleared": cleared}

    @router.get("/audit")
    async def audit_log(limit: int = 50) -> dict[str, Any]:  # noqa: ANN202
        """The tamper-evident audit trail + a live chain-integrity check."""
        entries = await app.state.audit.entries()
        chain_valid = await app.state.audit.verify_chain()
        rows = [{"seq": e.seq, "ts": e.ts, "actor": e.actor, "action": e.action,
                 "resource": e.resource, "outcome": e.outcome, "detail": e.detail,
                 "hash": e.entry_hash[:12]} for e in entries[-limit:]]
        return {"chain_valid": chain_valid, "count": len(entries), "entries": rows}

    app.include_router(router)
