"""Lightweight in-process metrics.

Counters and latency samples with simple aggregation (avg / p50 / p95). No
external system required — this is the "lightweight observability" the brief
asks for. In a bigger deployment you'd swap this for Prometheus; the call sites
(``inc`` / ``observe``) would stay the same.
"""

from __future__ import annotations

import threading
from collections import defaultdict


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
    return round(ordered[idx], 2)


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._latencies: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] += n

    def observe(self, name: str, value_ms: float) -> None:
        with self._lock:
            samples = self._latencies[name]
            samples.append(value_ms)
            if len(samples) > 1000:            # keep memory bounded
                del samples[0]

    def snapshot(self) -> dict:
        with self._lock:
            latency = {
                name: {
                    "count": len(v),
                    "avg_ms": round(sum(v) / len(v), 2) if v else 0.0,
                    "p50_ms": _pct(v, 50),
                    "p95_ms": _pct(v, 95),
                }
                for name, v in self._latencies.items()
            }
            return {"counters": dict(self._counters), "latency": latency}
