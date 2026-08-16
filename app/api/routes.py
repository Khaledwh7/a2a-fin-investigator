"""Human-facing REST API — the friendly front door for the UI.

This is an *implementation choice*, not A2A. The Streamlit UI doesn't speak
JSON-RPC; it POSTs a customer profile here, and this gateway drives the
investigation through the Orchestrator (in-process) and returns the task, the
execution trace (per-agent latency + cost) and a summary. In production this
route would sit behind the analyst's SSO; it is the trusted human boundary.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
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


def register_human_api(app: FastAPI) -> None:
    """Mount the human REST routes; reads dependencies off ``app.state``."""
    router = APIRouter(tags=["investigations"])

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:  # noqa: ANN202
        return {"status": "ok"}

    @router.get("/metrics")
    async def metrics() -> dict[str, Any]:  # noqa: ANN202
        return app.state.metrics.snapshot()

    @router.post("/investigations", response_model=InvestigationResponse)
    async def create_investigation(req: InvestigationRequest) -> InvestigationResponse:
        agent = app.state.agents[AgentRole.ORCHESTRATOR]
        message = Message(parts=[Part.from_data({"profile": req.profile.model_dump()})])
        # Drive the Orchestrator directly (this gateway is the trusted caller).
        result = await agent.send_message({"message": message.to_wire()},
                                          caller="user:analyst")
        task = Task.model_validate(result)
        trace_obj = app.state.trace_store.get(task.context_id)
        trace = trace_obj.to_dict() if trace_obj else None
        return InvestigationResponse(task=result, trace=trace,
                                     summary=_summary(task, trace))

    @router.post("/investigations/{task_id}/decision",
                 response_model=InvestigationResponse)
    async def submit_decision(task_id: str, req: DecisionRequest) -> InvestigationResponse:
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
                                          caller="user:analyst")
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
        return {"task": task.to_wire(),
                "trace": trace_obj.to_dict() if trace_obj else None}

    app.include_router(router)
