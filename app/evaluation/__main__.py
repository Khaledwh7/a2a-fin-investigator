"""CLI: run the evaluation suite and print a scorecard.

    python -m app.evaluation

Exits non-zero if the pass rate is below the threshold, so it can gate CI.
"""

from __future__ import annotations

import asyncio
import sys

from app.evaluation.runner import evaluate, format_scorecard

_THRESHOLD = 0.8


def main() -> int:
    card = asyncio.run(evaluate())
    print(format_scorecard(card))
    if card.pass_rate < _THRESHOLD:
        print(f"\nFAILED: pass rate {card.pass_rate:.0%} < {_THRESHOLD:.0%}")
        return 1
    print(f"\nOK: pass rate {card.pass_rate:.0%} >= {_THRESHOLD:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
