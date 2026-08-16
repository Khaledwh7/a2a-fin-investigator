"""SpecialistExecutor — shared behaviour for the five specialist agents.

Every specialist follows the same A2A rhythm:

    WORKING → (call tools, analyze) → emit findings Artifact → COMPLETED

So the base class owns that rhythm and each subclass only implements
``analyze(profile, context)`` (the domain logic) and ``summarize(findings)``
(a one-line human-readable status). The Orchestrator is *not* a specialist — it
has its own executor because it makes outbound A2A calls.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from app.a2a.executor import AgentExecutor, EventQueue, RequestContext
from app.a2a.types import Artifact, Part, TaskState
from app.agents.schemas import CustomerProfile, parse_profile
from app.tools.registry import ToolRegistry


class SpecialistExecutor(AgentExecutor):
    #: overridden by each subclass
    artifact_name: str = "findings"
    working_note: str = "analyzing"

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def run(self, ctx: RequestContext, events: EventQueue) -> None:
        await events.update_status(ctx.task, TaskState.WORKING, self.working_note)

        payload = ctx.message.first_data() or {}
        profile = parse_profile(payload.get("profile", payload))

        findings = self.analyze(profile, payload)

        artifact = Artifact(
            name=self.artifact_name,
            description=f"{self.role} findings",
            parts=[
                Part.from_text(self.summarize(findings)),
                Part.from_data(findings),
            ],
        )
        await events.emit_artifact(ctx.task, artifact)
        await events.update_status(ctx.task, TaskState.COMPLETED, "done")

    # -- subclass contract ------------------------------------------------
    @abstractmethod
    def analyze(self, profile: CustomerProfile, context: dict[str, Any]
                ) -> dict[str, Any]:
        """Return this agent's findings as a plain dict (goes into a data Part)."""

    @abstractmethod
    def summarize(self, findings: dict[str, Any]) -> str:
        """One-line human summary for the status message / UI."""
