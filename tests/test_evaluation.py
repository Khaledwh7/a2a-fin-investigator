"""Phase 7 — the evaluation harness itself.

Two things to prove: (1) the whole suite passes on the real system, and (2) the
evaluators actually *discriminate* — they must fail when the answer is wrong,
otherwise a green scorecard means nothing.
"""

from __future__ import annotations

from app.a2a.types import Artifact, Part, Task, TaskState, TaskStatus
from app.agents.schemas import CustomerProfile
from app.evaluation.dataset import SCENARIOS, Expectation, Scenario
from app.evaluation.evaluators import eval_answer_quality, eval_factual_consistency
from app.evaluation.runner import EvalHarness, evaluate
from app.observability.trace import InvestigationTrace


# --------------------------------------------------------------------------- #
# The full suite passes on the real system
# --------------------------------------------------------------------------- #
async def test_full_eval_suite_passes():
    card = await evaluate()
    assert len(card.results) == len(SCENARIOS)
    assert card.pass_rate == 1.0
    # Every dimension averages well above the noise floor.
    for dim, avg in card.by_dimension.items():
        assert avg >= 0.9, f"{dim} averaged only {avg}"


async def test_bands_match_expected():
    """Routing/scoring lands each scenario in the right band (CRITICAL…LOW)."""
    harness = EvalHarness()
    try:
        by_id = {sc.id: sc for sc in SCENARIOS}
        for sid in ["sanctioned_structurer", "clean_customer", "pep_high_risk_country",
                    "layering_ring", "prompt_injection"]:
            sc = by_id[sid]
            task, _trace = await harness.run(sc.profile)
            report = next(a for a in task.artifacts
                          if a.name == "investigation_report").first_data()
            assert report["risk_band"] == sc.expect.risk_band, sid
    finally:
        await harness.aclose()


# --------------------------------------------------------------------------- #
# The evaluators discriminate (fail when they should)
# --------------------------------------------------------------------------- #
async def test_answer_quality_fails_on_wrong_expected_band():
    harness = EvalHarness()
    try:
        # Clean customer, but we mislabel the expectation as CRITICAL.
        clean = CustomerProfile(full_name="John Smith", country="United Kingdom",
                                date_of_birth="1985-01-01", notes="salary")
        mislabelled = Scenario("bad", "mislabelled", clean,
                               Expectation(risk_band="CRITICAL", sanctions_hit=True,
                                           sar_recommended=True))
        task, trace = await harness.run(clean)
        score = eval_answer_quality(mislabelled, task, trace)
        assert score.passed is False        # must NOT rubber-stamp a wrong label
        assert "band_correct" in score.detail
    finally:
        await harness.aclose()


def test_factual_consistency_catches_contradiction():
    """A report that contradicts the risk finding must fail the consistency check."""
    task = Task()
    task.status = TaskStatus(state=TaskState.COMPLETED)
    task.artifacts = [
        Artifact(name="sanctions_findings", parts=[Part.from_data({"hit": False})]),
        Artifact(name="risk_assessment",
                 parts=[Part.from_data({"risk_score": 80, "risk_band": "HIGH",
                                        "sar_recommended": True})]),
        # Report LIES: says LOW/10 when the risk finding says HIGH/80.
        Artifact(name="investigation_report",
                 parts=[Part.from_text("## Summary\n## KYC\n## AML\n## Sanctions"),
                        Part.from_data({"risk_score": 10, "risk_band": "LOW",
                                        "sar_recommended": False})]),
    ]
    sc = Scenario("x", "x", CustomerProfile(full_name="x"),
                  Expectation(risk_band="LOW", sanctions_hit=False, sar_recommended=False))
    trace = InvestigationTrace(context_id="c", task_id="t", customer="x")
    result = eval_factual_consistency(sc, task, trace)
    assert result.passed is False
    assert result.score < 1.0
