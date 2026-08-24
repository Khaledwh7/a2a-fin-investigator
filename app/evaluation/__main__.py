"""CLI: run the evaluation suite and print a scorecard.

    python -m app.evaluation

Exits non-zero if the suite regresses, so it can gate CI. Two gates, because
they fail for different reasons:

* **pass rate** — the system did what the spec says (routing, consistency,
  latency, cost).
* **detection precision / recall** — it found what is there and left alone what
  is not. A model that flagged every customer would sail through the first gate
  and fail this one.
"""

from __future__ import annotations

import asyncio
import sys

from app.evaluation.runner import evaluate, format_scorecard

_PASS_RATE_THRESHOLD = 0.8
_PRECISION_THRESHOLD = 0.90      # false positives bury an analyst
_RECALL_THRESHOLD = 0.90         # misses are the ones that reach a regulator


def main() -> int:
    card = asyncio.run(evaluate())
    print(format_scorecard(card))

    failures: list[str] = []
    if card.pass_rate < _PASS_RATE_THRESHOLD:
        failures.append(f"pass rate {card.pass_rate:.0%} "
                        f"< {_PASS_RATE_THRESHOLD:.0%}")

    totals = card.detection.totals
    if totals.precision < _PRECISION_THRESHOLD:
        failures.append(f"detection precision {totals.precision:.2f} "
                        f"< {_PRECISION_THRESHOLD:.2f} ({totals.fp} false positives)")
    if totals.recall < _RECALL_THRESHOLD:
        failures.append(f"detection recall {totals.recall:.2f} "
                        f"< {_RECALL_THRESHOLD:.2f} ({totals.fn} missed)")

    if failures:
        print("\nFAILED: " + "; ".join(failures))
        return 1

    print(f"\nOK: pass rate {card.pass_rate:.0%} >= {_PASS_RATE_THRESHOLD:.0%} · "
          f"detection precision {totals.precision:.2f} / recall {totals.recall:.2f} "
          f"over {len(card.results)} scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
