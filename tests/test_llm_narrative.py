"""The optional LLM narrative layer.

The system ships deterministic and the ``anthropic`` package is not installed by
default, so every test here drives a stub client through the injection seam.
What matters is the contract around the model call, not the model: the request
carries the configured effort, real token usage reaches the artifact, and *any*
failure degrades to the deterministic template rather than losing the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.a2a.executor import EventQueue, RequestContext
from app.a2a.types import Message, Part, Task, TaskArtifactUpdateEvent
from app.agents import llm
from app.agents.reporting import ReportingExecutor
from app.agents.schemas import CustomerProfile
from app.config import get_settings
from app.tools.finance import build_default_registry


# --------------------------------------------------------------------------- #
# A stand-in for anthropic.AsyncAnthropic
# --------------------------------------------------------------------------- #
@dataclass
class _Block:
    type: str
    text: str


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


class _Response:
    def __init__(self, text: str, in_tokens: int, out_tokens: int) -> None:
        self.content = [_Block("thinking", "(reasoning)"), _Block("text", text)]
        self.usage = _Usage(in_tokens, out_tokens)


class StubClient:
    """Records the request it was given and returns a canned response."""

    def __init__(self, *, text: str = "Narrative from the model.",
                 error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._text = text
        self._error = error
        self.closed = False
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, outer: StubClient) -> None:
            self._outer = outer

        async def create(self, **kwargs: Any) -> _Response:
            self._outer.calls.append(kwargs)
            if self._outer._error is not None:
                raise self._outer._error
            return _Response(self._outer._text, 1234, 56)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def llm_on(monkeypatch):
    """Enable the LLM with a stub client; always restore global state."""
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.setenv("LLM_EFFORT", "low")
    get_settings.cache_clear()

    def _install(**kwargs: Any) -> StubClient:
        client = StubClient(**kwargs)
        llm.set_client(client)
        return client

    yield _install
    llm.set_client(None)
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# narrate()
# --------------------------------------------------------------------------- #
async def test_narrate_uses_the_model_and_reports_real_token_usage(llm_on):
    client = llm_on(text="Subject shows structuring across four deposits.")

    result = await llm.narrate(prompt="Write it.", system="You are an investigator.")

    assert result is not None and result.used_llm is True
    assert result.text == "Subject shows structuring across four deposits."
    # Usage is read from the response, never estimated.
    assert (result.input_tokens, result.output_tokens) == (1234, 56)

    request = client.calls[0]
    assert request["model"] == "claude-opus-5"
    # Effort is the supported depth/cost control and belongs inside output_config.
    assert request["output_config"] == {"effort": "low"}
    assert request["system"] == "You are an investigator."
    assert request["messages"] == [{"role": "user", "content": "Write it."}]
    # `thinking` is on by default on this model and budget_tokens is rejected,
    # so neither may be sent.
    assert "budget_tokens" not in request
    assert "thinking" not in request


async def test_narrate_reads_only_text_blocks(llm_on):
    """A response interleaves thinking and text; only the text is the narrative."""
    llm_on(text="Just the prose.")
    result = await llm.narrate(prompt="p", system="s")
    assert result is not None
    assert "(reasoning)" not in result.text
    assert result.text == "Just the prose."


async def test_narrate_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert await llm.narrate(prompt="p", system="s") is None
    finally:
        get_settings.cache_clear()


async def test_narrate_returns_none_without_an_api_key(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        assert await llm.narrate(prompt="p", system="s") is None
    finally:
        get_settings.cache_clear()


async def test_narrate_degrades_on_any_api_failure(llm_on):
    """A model outage must not cost us the report — the caller falls back."""
    llm_on(error=RuntimeError("529 overloaded"))
    assert await llm.narrate(prompt="p", system="s") is None


async def test_missing_anthropic_package_degrades_quietly(monkeypatch):
    """The package is optional; its absence behaves like any other failure."""
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    get_settings.cache_clear()
    llm.set_client(None)
    try:
        # The real package is not installed in this environment.
        assert await llm.narrate(prompt="p", system="s") is None
    finally:
        get_settings.cache_clear()


async def test_aclose_releases_the_shared_client(llm_on):
    client = llm_on()
    await llm.narrate(prompt="p", system="s")
    await llm.aclose()
    assert client.closed is True


async def test_client_is_reused_across_calls(llm_on):
    """One client per process — a pool per investigation would leak."""
    client = llm_on()
    await llm.narrate(prompt="one", system="s")
    await llm.narrate(prompt="two", system="s")
    assert len(client.calls) == 2


# --------------------------------------------------------------------------- #
# The Reporting agent's use of it
# --------------------------------------------------------------------------- #
async def _report(profile: CustomerProfile) -> tuple[str, dict | None]:
    """Run the Reporting agent and return (markdown, artifact llm metadata)."""
    executor = ReportingExecutor(build_default_registry())
    message = Message(parts=[Part.from_data({
        "profile": profile.model_dump(),
        "kyc": {"data_quality_score": 100},
        "aml": {"ledger_available": False, "attested_observations": []},
        "sanctions": {}, "fraud": {"assessed": False},
        "risk": {"risk_score": 12, "risk_band": "LOW", "decision": "APPROVE"},
    })])
    task = Task()
    events = EventQueue()
    await executor.run(RequestContext(message=message, task=task), events)
    await events.close()

    async for event in events.events():
        if isinstance(event, TaskArtifactUpdateEvent):
            artifact = event.artifact
            return artifact.parts[0].text, (artifact.metadata or {}).get("llm")
    raise AssertionError("the reporting agent emitted no artifact")


async def test_report_uses_the_llm_narrative_and_records_its_cost(llm_on):
    llm_on(text="The subject presents no adverse findings.")
    profile = CustomerProfile(full_name="Nora Bishop", country="Ireland")

    markdown, usage = await _report(profile)

    assert "The subject presents no adverse findings." in markdown
    # Token usage rides on the artifact so the trace can price the run.
    assert usage == {"model": "claude-opus-5", "input_tokens": 1234,
                     "output_tokens": 56}
    # And the report says where its narrative came from.
    assert "LLM (claude-opus-5, 1234 in / 56 out tokens)" in markdown


async def test_report_still_produced_when_the_model_fails(llm_on):
    """The reliability property: the report is always produced."""
    llm_on(error=TimeoutError("read timeout"))
    profile = CustomerProfile(full_name="Nora Bishop", country="Ireland")

    markdown, usage = await _report(profile)

    assert usage is None
    assert "deterministic template (no LLM used)" in markdown
    assert "Nora Bishop" in markdown
    assert markdown.startswith("# Investigation Report - Nora Bishop")
