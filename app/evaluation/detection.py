"""Detection-level scoring — precision and recall per detector.

The scenario evaluators answer "did the system do what the spec says". They
cannot answer the question a reviewer actually cares about: *does it find
financial crime, and does it leave clean customers alone?* A suite of positives
only measures recall; a model that flagged everything would pass it.

So each scenario is labelled with the detectors that must fire, and everything
in ``DETECTORS`` that is not labelled is expected **not** to fire. Running the
suite then yields a confusion matrix per detector — which turns "100% pass rate"
into numbers that mean something, and makes a false positive as visible as a
miss.

The detectors below are read from the agents' own artifacts, so the benchmark
scores the shipped findings rather than a parallel reimplementation of them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.a2a.types import Task


def _data(task: Task, artifact: str) -> dict[str, Any]:
    art = next((a for a in task.artifacts if a.name == artifact), None)
    return (art.first_data() if art else {}) or {}


# name -> predicate over the (kyc, aml, sanctions, fraud) findings
DETECTORS: dict[str, Callable[[dict, dict, dict, dict], bool]] = {
    "structuring":         lambda k, a, s, f: bool(a.get("structuring_detected")),
    "rapid_movement":      lambda k, a, s, f: bool(a.get("rapid_movement")),
    "cash_intensive":      lambda k, a, s, f: a.get("cash_ratio", 0) >= 0.3,
    "crypto_exposure":     lambda k, a, s, f: a.get("crypto_total", 0) > 0,
    "high_risk_counterparty": lambda k, a, s, f: bool(a.get("high_risk_counterparties")),
    "velocity_spike":      lambda k, a, s, f: bool(a.get("high_velocity")),
    "volume_over_expected": lambda k, a, s, f: bool(a.get("over_expected_volume")),
    "sanctions_hit":       lambda k, a, s, f: bool(s.get("hit")),
    "sanctions_possible":  lambda k, a, s, f: s.get("match_tier") == "POSSIBLE",
    "beneficiary_hit":     lambda k, a, s, f: bool(s.get("counterparty_hit")),
    "blocked_country":     lambda k, a, s, f: bool(s.get("blocked_country_exposure")),
    "pep":                 lambda k, a, s, f: bool(k.get("is_pep")),
    "fraud_alert":         lambda k, a, s, f: bool(f.get("fraud_alert")),
}


def observed_detections(task: Task) -> set[str]:
    """Which detectors fired on this investigation, per the agents' artifacts."""
    kyc = _data(task, "kyc_findings")
    aml = _data(task, "aml_findings")
    sanctions = _data(task, "sanctions_findings")
    fraud = _data(task, "fraud_findings")
    return {name for name, fired in DETECTORS.items()
            if fired(kyc, aml, sanctions, fraud)}


@dataclass
class DetectorStats:
    name: str
    tp: int = 0          # should fire, did
    fp: int = 0          # should not fire, did          ← the expensive mistake
    fn: int = 0          # should fire, did not          ← the dangerous one
    tn: int = 0

    @property
    def precision(self) -> float:
        fired = self.tp + self.fp
        return round(self.tp / fired, 3) if fired else 1.0

    @property
    def recall(self) -> float:
        expected = self.tp + self.fn
        return round(self.tp / expected, 3) if expected else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return round(2 * p * r / (p + r), 3) if (p + r) else 0.0

    @property
    def exercised(self) -> bool:
        """True when the suite actually contains a positive for this detector."""
        return (self.tp + self.fn) > 0


@dataclass
class DetectionMatrix:
    """Confusion matrix per detector, accumulated across the whole suite."""

    stats: dict[str, DetectorStats] = field(default_factory=dict)
    mistakes: list[str] = field(default_factory=list)

    def add(self, scenario_id: str, expected: set[str], observed: set[str]) -> None:
        for name in DETECTORS:
            s = self.stats.setdefault(name, DetectorStats(name))
            should, did = name in expected, name in observed
            if should and did:
                s.tp += 1
            elif should and not did:
                s.fn += 1
                self.mistakes.append(f"{scenario_id}: MISSED {name}")
            elif did:
                s.fp += 1
                self.mistakes.append(f"{scenario_id}: FALSE POSITIVE {name}")
            else:
                s.tn += 1

    # -- suite-level aggregates -------------------------------------------
    @property
    def totals(self) -> DetectorStats:
        total = DetectorStats("overall")
        for s in self.stats.values():
            total.tp += s.tp
            total.fp += s.fp
            total.fn += s.fn
            total.tn += s.tn
        return total

    @property
    def exercised(self) -> list[DetectorStats]:
        """Detectors the suite actually tests — the rest have no positives yet."""
        return [s for s in self.stats.values() if s.exercised]

    @property
    def unexercised(self) -> list[str]:
        return sorted(n for n, s in self.stats.items() if not s.exercised)

    def to_dict(self) -> dict[str, Any]:
        total = self.totals
        return {
            "precision": total.precision,
            "recall": total.recall,
            "f1": total.f1,
            "confusion": {"tp": total.tp, "fp": total.fp,
                          "fn": total.fn, "tn": total.tn},
            "per_detector": {
                s.name: {"precision": s.precision, "recall": s.recall, "f1": s.f1,
                         "tp": s.tp, "fp": s.fp, "fn": s.fn, "tn": s.tn}
                for s in sorted(self.stats.values(), key=lambda x: x.name)
            },
            "unexercised": self.unexercised,
            "mistakes": self.mistakes,
        }

    def render(self) -> list[str]:
        total = self.totals
        lines = [
            "",
            "  DETECTION BENCHMARK  (per-detector, across the whole suite)",
            "  " + "-" * 74,
            f"  {'detector':<26}{'prec':>7}{'recall':>8}{'F1':>7}"
            f"{'TP':>5}{'FP':>5}{'FN':>5}{'TN':>5}",
        ]
        for s in sorted(self.exercised, key=lambda x: (x.f1, x.name)):
            flag = "  <-- " + ("misses" if s.fn else "false positives") \
                if s.f1 < 1.0 else ""
            lines.append(f"  {s.name:<26}{s.precision:>7.2f}{s.recall:>8.2f}"
                         f"{s.f1:>7.2f}{s.tp:>5}{s.fp:>5}{s.fn:>5}{s.tn:>5}{flag}")
        lines += [
            "  " + "-" * 74,
            f"  {'OVERALL':<26}{total.precision:>7.2f}{total.recall:>8.2f}"
            f"{total.f1:>7.2f}{total.tp:>5}{total.fp:>5}{total.fn:>5}{total.tn:>5}",
        ]
        if self.unexercised:
            lines.append(f"  not exercised by any positive case: "
                         f"{', '.join(self.unexercised)}")
        for mistake in self.mistakes:
            lines.append(f"    ! {mistake}")
        return lines
