"""Thin client for the investigation REST API.

The UI talks ONLY to this HTTP surface — never to the agents directly. That's
the clean separation the architecture diagram shows: UI → REST → A2A.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import httpx
from httpx_sse import connect_sse

DEFAULT_API = os.getenv("API_BASE_URL", "http://localhost:8000")


class ApiError(Exception):
    pass


def _client(base_url: str, analyst: str | None = None) -> httpx.Client:
    """An HTTP client carrying the analyst's identity for the audit trail."""
    headers = {"X-Analyst": analyst} if analyst else {}
    return httpx.Client(base_url=base_url, timeout=120.0, headers=headers)


def health(base_url: str) -> bool:
    return health_detail(base_url).get("status") == "ok"


def health_detail(base_url: str) -> dict[str, Any]:
    """Full /healthz payload — includes any peers being served in-process."""
    try:
        with _client(base_url) as c:
            return c.get("/healthz").json()
    except Exception:
        return {}


def run_investigation(base_url: str, profile: dict[str, Any],
                      analyst: str | None = None) -> dict[str, Any]:
    try:
        with _client(base_url, analyst) as c:
            resp = c.post("/investigations", json={"profile": profile})
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach API at {base_url}: {exc}") from exc
    if resp.status_code == 422:
        raise ApiError(f"Invalid profile: {resp.json().get('detail')}")
    if resp.status_code != 200:
        raise ApiError(f"API returned HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def stream_investigation(base_url: str, profile: dict[str, Any],
                         analyst: str | None = None) -> Iterator[dict[str, Any]]:
    """Yield the investigation's events as they happen.

    Frames: ``accepted`` → ``status`` / ``artifact`` (repeating) → ``done``.
    The ``done`` frame carries the same task/trace/summary the blocking endpoint
    returns, so callers can render a streamed run exactly like a replayed one.
    """
    try:
        with _client(base_url, analyst) as c, connect_sse(
                c, "POST", "/investigations/stream", json={"profile": profile}
        ) as source:
            if source.response.status_code != 200:
                source.response.read()
                raise ApiError(f"API returned HTTP {source.response.status_code}: "
                               f"{source.response.text[:300]}")
            for sse in source.iter_sse():
                if sse.data:
                    yield json.loads(sse.data)
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach API at {base_url}: {exc}") from exc


def list_investigations(base_url: str, limit: int = 50) -> list[dict[str, Any]]:
    with _client(base_url) as c:
        resp = c.get("/investigations", params={"limit": limit})
    return resp.json() if resp.status_code == 200 else []


def submit_decision(base_url: str, task_id: str, action: str, note: str = "",
                    override_band: str | None = None,
                    analyst: str | None = None) -> dict[str, Any]:
    """Human-in-the-loop: approve / override / close a paused investigation."""
    body = {"action": action, "note": note, "override_band": override_band}
    try:
        with _client(base_url, analyst) as c:
            resp = c.post(f"/investigations/{task_id}/decision", json=body)
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach API at {base_url}: {exc}") from exc
    if resp.status_code == 409:
        raise ApiError("This investigation is no longer awaiting review.")
    if resp.status_code != 200:
        raise ApiError(f"API returned HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def get_investigation(base_url: str, task_id: str) -> dict[str, Any] | None:
    with _client(base_url) as c:
        resp = c.get(f"/investigations/{task_id}")
    return resp.json() if resp.status_code == 200 else None


def delete_investigation(base_url: str, task_id: str,
                         analyst: str | None = None) -> dict[str, Any]:
    """Remove one case and every agent task in its context."""
    try:
        with _client(base_url, analyst) as c:
            resp = c.delete(f"/investigations/{task_id}")
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach API at {base_url}: {exc}") from exc
    if resp.status_code == 404:
        raise ApiError("That investigation no longer exists.")
    if resp.status_code != 200:
        raise ApiError(f"API returned HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def clear_audit(base_url: str, analyst: str | None = None) -> dict[str, Any]:
    """Wipe the audit log and start a fresh hash chain."""
    try:
        with _client(base_url, analyst) as c:
            resp = c.delete("/audit")
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach API at {base_url}: {exc}") from exc
    if resp.status_code != 200:
        raise ApiError(f"API returned HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def get_metrics(base_url: str) -> dict[str, Any]:
    with _client(base_url) as c:
        resp = c.get("/metrics")
    return resp.json() if resp.status_code == 200 else {}


def get_audit(base_url: str, limit: int = 100) -> dict[str, Any]:
    with _client(base_url) as c:
        resp = c.get("/audit", params={"limit": limit})
    return resp.json() if resp.status_code == 200 else {}
