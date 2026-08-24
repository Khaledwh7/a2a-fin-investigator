"""Evaluation runner — drive every scenario through the system and score it.

Runs each scenario as a real investigation (in-process, through the same
Orchestrator the UI uses), applies all six evaluators, and aggregates a
scorecard: per-scenario results plus overall pass-rate and per-dimension
averages.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from app.a2a.types import Message, Part, Task
from app.agents.schemas import CustomerProfile
from app.api.factory import build_app, default_client
from app.config import AgentRole, Settings
from app.evaluation.dataset import SCENARIOS, Scenario
from app.evaluation.detection import DetectionMatrix, observed_detections
from app.evaluation.evaluators import EVALUATORS, DimensionScore
from app.observability.trace import InvestigationTrace

# Dimensions that must pass for a scenario to count as an overall pass. Quality
# is scored but treated as soft (heuristic).
_HARD_DIMS = {"task_success", "agent_routing", "factual_consistency", "latency", "cost"}


@dataclass
class ScenarioResult:
    scenario_id: str
    scores: list[DimensionScore]
    overall_score: float
    passed: bool


@dataclass
class Scorecard:
    results: list[ScenarioResult] = field(default_factory=list)
    #: suite-level confusion matrix per detector (precision / recall / F1)
    detection: DetectionMatrix = field(default_factory=DetectionMatrix)

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return round(sum(r.passed for r in self.results) / len(self.results), 3)

    @property
    def by_dimension(self) -> dict[str, float]:
        totals: dict[str, list[float]] = {}
        for r in self.results:
            for s in r.scores:
                totals.setdefault(s.dimension, []).append(s.score)
        return {d: round(sum(v) / len(v), 3) for d, v in totals.items()}

    def to_dict(self) -> dict:
        return {
            "pass_rate": self.pass_rate,
            "by_dimension": self.by_dimension,
            "detection": self.detection.to_dict(),
            "scenarios": [
                {"id": r.scenario_id, "passed": r.passed,
                 "overall_score": r.overall_score,
                 "scores": [{"dimension": s.dimension, "score": s.score,
                             "passed": s.passed, "detail": s.detail} for s in r.scores]}
                for r in self.results
            ],
        }


class EvalHarness:
    """Owns a wired app whose Orchestrator calls specialists in-process."""

    def __init__(self, settings: Settings | None = None) -> None:
        # Evaluation scores the AUTOMATED pipeline end to end, so the human-review
        # gate is disabled here (it's covered by tests/test_human_in_the_loop.py).
        self.settings = settings or Settings(require_human_review=False)
        self.app = build_app(self.settings)
        self._transport = httpx.ASGITransport(app=self.app)
        # The orchestrator keeps its real credentials and card verification —
        # only the socket is swapped — so the suite scores the configuration the
        # app actually ships with.
        client = default_client(
            self.settings, token_service=self.app.state.token_service,
            secret=self.settings.jwt_secret.get_secret_value(),
            transport=self._transport)
        client.set_loopback(self._transport)
        self.app.state.orchestrator.set_client(client)
        self.app.state.a2a_client = client

    async def run(self, profile: CustomerProfile) -> tuple[Task, InvestigationTrace]:
        agent = self.app.state.agents[AgentRole.ORCHESTRATOR]
        msg = Message(parts=[Part.from_data({"profile": profile.model_dump()})])
        result = await agent.send_message({"message": msg.to_wire()}, caller="eval")
        task = Task.model_validate(result)
        trace = self.app.state.trace_store.get(task.context_id)
        return task, trace

    async def aclose(self) -> None:
        await self.app.state.orchestrator.aclose()


async def evaluate(scenarios: list[Scenario] | None = None,
                   harness: EvalHarness | None = None) -> Scorecard:
    scenarios = scenarios or SCENARIOS
    owns_harness = harness is None
    harness = harness or EvalHarness()
    card = Scorecard()
    try:
        for sc in scenarios:
            task, trace = await harness.run(sc.profile)
            scores = [ev(sc, task, trace) for ev in EVALUATORS]
            overall = round(sum(s.score for s in scores) / len(scores), 3)
            passed = all(s.passed for s in scores if s.dimension in _HARD_DIMS)
            card.results.append(ScenarioResult(sc.id, scores, overall, passed))
            card.detection.add(sc.id, set(sc.expect.detections),
                               observed_detections(task))
    finally:
        if owns_harness:
            await harness.aclose()
    return card


def format_scorecard(card: Scorecard) -> str:
    lines = ["", "=" * 76, "  EVALUATION SCORECARD", "=" * 76]
    for r in card.results:
        flag = "PASS" if r.passed else "FAIL"
        lines.append(f"\n[{flag}] {r.scenario_id}   (overall {r.overall_score:.2f})")
        for s in r.scores:
            mark = "PASS" if s.passed else "FAIL"
            lines.append(f"    [{mark}] {s.dimension:20} {s.score:.2f}  {s.detail}")
    lines.append("\n" + "-" * 76)
    lines.append(f"  OVERALL PASS RATE: {card.pass_rate:.0%}   "
                 f"({sum(r.passed for r in card.results)}/{len(card.results)} scenarios)")
    dims = card.by_dimension
    lines.append("  Per-dimension avg: "
                 + "  ".join(f"{d}={v:.2f}" for d, v in dims.items()))
    lines += card.detection.render()
    lines.append("=" * 76)
    return "\n".join(lines)
