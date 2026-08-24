"""Token accounting and cost estimation.

Two numbers the UI shows:

  * **LLM tokens / cost** — REAL usage, reported by the Reporting agent when the
    LLM is enabled (from the Anthropic ``usage`` block). Zero in deterministic
    mode, because we genuinely spend nothing.
  * **Estimated tokens** — a heuristic (~4 chars/token) over the payloads that
    flow between agents. It gives the dashboard a meaningful "work processed"
    number even with no LLM, and demonstrates the accounting path. Always
    labelled "estimated".

⚠️ PRICING BELOW is the published list price (USD per 1M tokens) at the time of
writing. Re-check it against the current Anthropic price list before quoting
real costs — an unknown model falls back to 0.0 rather than guessing.
"""

from __future__ import annotations

import json
from typing import Any

# (input_per_million, output_per_million) — ILLUSTRATIVE, verify before use.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = PRICING_USD_PER_MTOK.get(model, (0.0, 0.0))
    return round((input_tokens / 1_000_000) * in_price
                 + (output_tokens / 1_000_000) * out_price, 6)


def estimate_tokens(text: str) -> int:
    """Rough token count for text (~4 characters per token)."""
    return max(0, len(text) // 4)


def estimate_payload_tokens(obj: Any) -> int:
    """Estimated tokens to represent an arbitrary JSON-able payload."""
    try:
        return estimate_tokens(json.dumps(obj, default=str))
    except (TypeError, ValueError):
        return estimate_tokens(str(obj))
