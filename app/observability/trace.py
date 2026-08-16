"""Distributed-ish execution trace for one investigation.

Each investigation gets an ``InvestigationTrace`` made of ``Span``s — one per
agent-to-agent call — recording latency, status, errors and token/cost. It's a
lightweight, in-process stand-in for OpenTelemetry that gives the UI exactly
what it needs to draw the A2A flow with per-agent timing and a cost/latency
summary. The trace is keyed by the A2A ``contextId``, so it lines up 1:1 with
the shared investigation context.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field


def _now_ms() -> float:
    return round(time.time() * 1000, 1)


@dataclass
class Span:
    name: str
    kind: str                     # "a2a_call" | "agent" | "tool"
    agent: str | None = None
    start_ms: float = field(default_factory=_now_ms)
    duration_ms: float = 0.0
    status: str = "ok"            # "ok" | "error"
    error: str | None = None
    llm_tokens: int = 0
    est_tokens: int = 0
    cost_usd: float = 0.0
    detail: dict = field(default_factory=dict)


@dataclass
class InvestigationTrace:
    context_id: str
    task_id: str
    customer: str
    started_at: float = field(default_factory=_now_ms)
    wall_ms: float = 0.0
    spans: list[Span] = field(default_factory=list)

    @contextmanager
    def span(self, name: str, kind: str, agent: str | None = None):
        sp = Span(name=name, kind=kind, agent=agent)
        t0 = time.perf_counter()
        try:
            yield sp
        finally:
            sp.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            self.spans.append(sp)

    # -- aggregates the UI shows -----------------------------------------
    @property
    def agent_calls(self) -> int:
        return sum(1 for s in self.spans if s.kind == "a2a_call")

    @property
    def errors(self) -> int:
        return sum(1 for s in self.spans if s.status == "error")

    @property
    def llm_tokens(self) -> int:
        return sum(s.llm_tokens for s in self.spans)

    @property
    def est_tokens(self) -> int:
        return sum(s.est_tokens for s in self.spans)

    @property
    def cost_usd(self) -> float:
        return round(sum(s.cost_usd for s in self.spans), 6)

    def to_dict(self) -> dict:
        return {
            "context_id": self.context_id,
            "task_id": self.task_id,
            "customer": self.customer,
            "started_at": self.started_at,
            "wall_ms": self.wall_ms,
            "agent_calls": self.agent_calls,
            "errors": self.errors,
            "llm_tokens": self.llm_tokens,
            "est_tokens": self.est_tokens,
            "cost_usd": self.cost_usd,
            "spans": [asdict(s) for s in self.spans],
        }


class TraceStore:
    """In-memory ring of the most recent traces, keyed by contextId."""

    def __init__(self, max_traces: int = 200) -> None:
        self._traces: dict[str, InvestigationTrace] = {}
        self._order: list[str] = []
        self._max = max_traces

    def put(self, trace: InvestigationTrace) -> None:
        if trace.context_id not in self._traces:
            self._order.append(trace.context_id)
        self._traces[trace.context_id] = trace
        while len(self._order) > self._max:
            evicted = self._order.pop(0)
            self._traces.pop(evicted, None)

    def get(self, context_id: str) -> InvestigationTrace | None:
        return self._traces.get(context_id)
