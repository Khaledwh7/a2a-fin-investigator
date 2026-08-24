"""Investigation repository — read model for the UI / history.

An "investigation" is our own concept (not A2A): it's the Orchestrator's root
Task, which carries every specialist artifact plus the final report. So listing
investigations = listing orchestrator-role tasks, newest first, with a small
summary lifted out of the report artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.a2a.types import Task
from app.config import AgentRole
from app.database.models import TaskRecord


@dataclass
class InvestigationSummary:
    task_id: str
    context_id: str
    state: str
    created_at: str
    customer: str | None
    risk_score: int | None
    risk_band: str | None
    sar_recommended: bool | None


class InvestigationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def list(self, limit: int = 50) -> list[InvestigationSummary]:
        stmt = (select(TaskRecord)
                .where(TaskRecord.agent_role == AgentRole.ORCHESTRATOR.value)
                .order_by(TaskRecord.created_at.desc())
                .limit(limit))
        async with self._sf() as s:
            rows = (await s.execute(stmt)).scalars().all()
        return [self._summarize(r) for r in rows]

    async def get(self, task_id: str) -> Task | None:
        async with self._sf() as s:
            rec = await s.get(TaskRecord, task_id)
        if rec is None or rec.agent_role != AgentRole.ORCHESTRATOR.value:
            return None
        return Task.model_validate(rec.task_json)

    # -- helpers ----------------------------------------------------------
    def _summarize(self, rec: TaskRecord) -> InvestigationSummary:
        summary = self._report_summary(rec.task_json)
        return InvestigationSummary(
            task_id=rec.id,
            context_id=rec.context_id,
            state=rec.state,
            created_at=rec.created_at.isoformat() if rec.created_at else "",
            customer=summary.get("customer"),
            risk_score=summary.get("risk_score"),
            risk_band=summary.get("risk_band"),
            sar_recommended=summary.get("sar_recommended"),
        )

    @staticmethod
    def _report_summary(task_json: dict[str, Any]) -> dict[str, Any]:
        """Summary fields for the history row.

        Prefers the final report, but falls back to the risk assessment and the
        request history. A case paused at INPUT_REQUIRED has no report yet — and
        those are precisely the ones an analyst has to come back and action, so
        they must not list as blank rows.
        """
        by_name = {a.get("name"): a for a in task_json.get("artifacts", [])}

        def data_of(name: str) -> dict[str, Any]:
            for part in (by_name.get(name) or {}).get("parts", []):
                if isinstance(part.get("data"), dict):
                    return part["data"]
            return {}

        report = data_of("investigation_report")
        if report:
            return report
        risk = data_of("risk_assessment")
        return {"customer": _customer_name(task_json),
                "risk_score": risk.get("risk_score"),
                "risk_band": risk.get("risk_band"),
                "sar_recommended": risk.get("sar_recommended")}


def _customer_name(task_json: dict[str, Any]) -> str | None:
    """Recover the subject's name from the request that started the task."""
    for message in task_json.get("history", []):
        for part in message.get("parts", []):
            data = part.get("data")
            if isinstance(data, dict) and isinstance(data.get("profile"), dict):
                return data["profile"].get("full_name")
    return None
