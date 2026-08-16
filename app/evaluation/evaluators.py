"""The six evaluators — one per dimension the brief asks for.

Each takes the scenario's ground truth plus the actual investigation output
(the Task and its execution trace) and returns a 0–1 score with a pass/fail and
a human-readable detail. They're deterministic — no LLM needed — which keeps the
eval reproducible and free to run in CI.

Dimensions: task success · agent routing · answer quality · factual consistency
· latency · cost.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.a2a.types import Task, TaskState
from app.evaluation.dataset import Scenario
from app.observability.trace import InvestigationTrace


@dataclass
class DimensionScore:
    dimension: str
    score: float          # 0.0 – 1.0
    passed: bool
    detail: str


def _artifact(task: Task, name: str):
    return next((a for a in task.artifacts if a.name == name), None)


def _report_data(task: Task) -> dict:
    art = _artifact(task, "investigation_report")
    return (art.first_data() if art else {}) or {}


def _report_text(task: Task) -> str:
    art = _artifact(task, "investigation_report")
    if not art:
        return ""
    return "\n".join(p.text for p in art.parts if p.text)


# --------------------------------------------------------------------------- #
def eval_task_success(sc: Scenario, task: Task, trace: InvestigationTrace) -> DimensionScore:
    completed = task.status.state == TaskState.COMPLETED
    got = {a.name for a in task.artifacts}
    want = {"kyc_findings", "aml_findings", "sanctions_findings",
            "risk_assessment", "investigation_report"}
    have_all = want.issubset(got)
    passed = completed and have_all
    return DimensionScore("task_success", 1.0 if passed else 0.0, passed,
                          f"state={task.status.state.value}, artifacts={sorted(got)}")


def eval_agent_routing(sc: Scenario, task: Task, trace: InvestigationTrace) -> DimensionScore:
    actual = [s.agent for s in trace.spans if s.kind == "a2a_call"]
    expected = sc.expect.expected_agents
    matched = sum(1 for a, e in zip(actual, expected, strict=False) if a == e)
    score = matched / len(expected) if expected else 0.0
    passed = actual == expected
    return DimensionScore("agent_routing", round(score, 3), passed,
                          f"expected={expected}, actual={actual}")


def eval_answer_quality(sc: Scenario, task: Task, trace: InvestigationTrace) -> DimensionScore:
    text = _report_text(task)
    data = _report_data(task)
    sanc = (_artifact(task, "sanctions_findings").first_data()
            if _artifact(task, "sanctions_findings") else {}) or {}
    # structural checks (is the report well-formed?) ...
    structural = {
        "has_summary": "## Summary" in text,
        "has_kyc": "## KYC" in text,
        "has_aml": "## AML" in text,
        "has_sanctions": "## Sanctions" in text,
        "has_recommendation": bool(data.get("recommendation")),
    }
    # ... and correctness checks against ground truth.
    correctness = {
        "band_correct": data.get("risk_band") == sc.expect.risk_band,
        "sanctions_correct": bool(sanc.get("hit")) == sc.expect.sanctions_hit,
        "sar_correct": bool(data.get("sar_recommended")) == sc.expect.sar_recommended,
    }
    checks = {**structural, **correctness}
    score = sum(checks.values()) / len(checks)
    passed = all(correctness.values()) and score >= 0.8
    failed = [k for k, v in checks.items() if not v]
    return DimensionScore("answer_quality", round(score, 3), passed,
                          f"failed_checks={failed or 'none'}")


def eval_factual_consistency(sc: Scenario, task: Task,
                             trace: InvestigationTrace) -> DimensionScore:
    """The report must not contradict the specialist findings it summarizes."""
    report = _report_data(task)
    risk = (_artifact(task, "risk_assessment").first_data()
            if _artifact(task, "risk_assessment") else {}) or {}
    sanc = (_artifact(task, "sanctions_findings").first_data()
            if _artifact(task, "sanctions_findings") else {}) or {}
    text = _report_text(task)

    checks = {
        "score_matches": report.get("risk_score") == risk.get("risk_score"),
        "band_matches": report.get("risk_band") == risk.get("risk_band"),
        "sar_matches": report.get("sar_recommended") == risk.get("sar_recommended"),
        # A confirmed sanctions hit must surface as a STRONG tier in the report.
        "sanctions_reflected": (not sanc.get("hit")) or ("Match tier: STRONG" in text),
    }
    for fact in sc.expect.must_mention:
        checks[f"mentions_{fact}"] = fact.lower() in text.lower()

    score = sum(checks.values()) / len(checks)
    passed = score == 1.0
    failed = [k for k, v in checks.items() if not v]
    return DimensionScore("factual_consistency", round(score, 3), passed,
                          f"failed_checks={failed or 'none'}")


def eval_latency(sc: Scenario, task: Task, trace: InvestigationTrace) -> DimensionScore:
    budget = sc.expect.latency_budget_ms
    actual = trace.wall_ms
    passed = actual <= budget
    return DimensionScore("latency", 1.0 if passed else 0.0, passed,
                          f"{actual:.0f}ms (budget {budget:.0f}ms)")


def eval_cost(sc: Scenario, task: Task, trace: InvestigationTrace) -> DimensionScore:
    budget = sc.expect.cost_budget_usd
    actual = trace.cost_usd
    passed = actual <= budget
    return DimensionScore("cost", 1.0 if passed else 0.0, passed,
                          f"${actual:.4f} (budget ${budget:.2f}), "
                          f"est_tokens={trace.est_tokens}, llm_tokens={trace.llm_tokens}")


EVALUATORS = [
    eval_task_success,
    eval_agent_routing,
    eval_answer_quality,
    eval_factual_consistency,
    eval_latency,
    eval_cost,
]
