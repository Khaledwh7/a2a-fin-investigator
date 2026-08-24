"""The evaluation harness itself.

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


async def test_detection_benchmark_has_no_misses_or_false_positives():
    """The suite measures detection, not just conformance.

    Precision matters as much as recall here: a detector that fired on every
    customer would pass a positives-only suite and bury an analyst in practice.
    """
    card = await evaluate()
    matrix = card.detection
    totals = matrix.totals

    assert totals.fn == 0, [m for m in matrix.mistakes if "MISSED" in m]
    assert totals.fp == 0, [m for m in matrix.mistakes if "FALSE POSITIVE" in m]
    assert totals.precision == 1.0 and totals.recall == 1.0
    # Negatives must actually outnumber positives, or "precision 1.0" is cheap.
    assert totals.tn > totals.tp

    # Every detector the system ships is exercised by at least one positive case,
    # so none of them can rot unnoticed behind a green scorecard.
    assert matrix.unexercised == [], matrix.unexercised


async def test_benchmark_would_catch_a_regression():
    """A guard on the guard: the matrix must react to a wrong answer."""
    from app.evaluation.detection import DETECTORS, DetectionMatrix

    matrix = DetectionMatrix()
    matrix.add("case_a", {"structuring"}, {"structuring"})          # correct
    matrix.add("case_b", {"structuring"}, set())                    # a miss
    matrix.add("case_c", set(), {"sanctions_hit"})                  # a false positive

    totals = matrix.totals
    assert totals.tp == 1 and totals.fn == 1 and totals.fp == 1
    assert matrix.stats["structuring"].recall == 0.5
    assert matrix.stats["sanctions_hit"].precision == 0.0
    assert any("MISSED structuring" in m for m in matrix.mistakes)
    assert any("FALSE POSITIVE sanctions_hit" in m for m in matrix.mistakes)
    # Detectors never asked to fire are not counted as perfect recall by accident.
    assert set(matrix.stats) == set(DETECTORS)


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
