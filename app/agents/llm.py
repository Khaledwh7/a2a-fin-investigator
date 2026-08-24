"""Optional LLM narrative layer.

The whole system runs deterministically with no API key. When ``LLM_ENABLED`` is
true and a key is present, the Reporting agent can ask Claude to write the
investigation narrative. This module isolates that dependency so:

  * importing it never requires the ``anthropic`` package (imported lazily), and
  * any LLM failure degrades gracefully to the deterministic template
    (a reliability property — the report is always produced).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.config import get_settings

# One client per process. The Anthropic SDK holds an HTTP connection pool, so
# building a fresh client per report would leak a pool per investigation.
_client: Any = None
_client_lock = asyncio.Lock()


async def _get_client() -> Any:
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                from anthropic import AsyncAnthropic
                settings = get_settings()
                _client = AsyncAnthropic(
                    api_key=settings.anthropic_api_key.get_secret_value())
    return _client


def set_client(client: Any) -> None:
    """Inject a preconfigured client.

    The seam that makes this layer testable without the ``anthropic`` package
    installed — and the hook for supplying a client with custom transport or
    credentials in a deployment that needs one.
    """
    global _client
    _client = client


async def aclose() -> None:
    """Release the shared client (called from the app's lifespan shutdown)."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


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
        # A missing `anthropic` package surfaces here as an ImportError from
        # _get_client and degrades to the template like any other failure —
        # there is no separate availability check to keep in sync.
        client = await _get_client()
        # Adaptive thinking is on by default on Claude Opus 5; `effort` is the
        # supported way to trade depth against cost and lives inside
        # `output_config`. `max_tokens` caps thinking AND response text together,
        # so the narrative budget below has to cover both.
        resp = await client.messages.create(
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            output_config={"effort": settings.llm_effort},
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
