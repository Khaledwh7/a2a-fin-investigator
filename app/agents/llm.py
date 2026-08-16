"""Optional LLM narrative layer.

The whole system runs deterministically with no API key. When ``LLM_ENABLED`` is
true and a key is present, the Reporting agent can ask Claude to write the
investigation narrative. This module isolates that dependency so:

  * importing it never requires the ``anthropic`` package (imported lazily), and
  * any LLM failure degrades gracefully to the deterministic template
    (a reliability property — the report is always produced).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings


@dataclass
class LLMResult:
    text: str
    used_llm: bool
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


async def narrate(prompt: str, system: str) -> LLMResult | None:
    """Return an LLM narrative, or None if LLM is disabled/unavailable.

    Returning None (rather than raising) lets the caller fall back to the
    deterministic template without special-casing errors.
    """
    settings = get_settings()
    if not settings.llm_enabled or settings.anthropic_api_key is None:
        return None

    try:
        from anthropic import AsyncAnthropic  # lazy import
    except ImportError:
        return None

    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
        # Adaptive thinking + effort is the current recommended pattern for
        # Claude Opus 5 / Sonnet 5.
        resp = await client.messages.create(
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return LLMResult(
            text=text,
            used_llm=True,
            model=settings.llm_model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
    except Exception:  # noqa: BLE001 — any failure → deterministic fallback
        return None
