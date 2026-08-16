"""Thin client for the investigation REST API.

The UI talks ONLY to this HTTP surface — never to the agents directly. That's
the clean separation the architecture diagram shows: UI → REST → A2A.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_API = os.getenv("API_BASE_URL", "http://localhost:8000")


class ApiError(Exception):
    pass


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=120.0)


def health(base_url: str) -> bool:
    try:
        with _client(base_url) as c:
            return c.get("/healthz").json().get("status") == "ok"
    except Exception:
        return False


def run_investigation(base_url: str, profile: dict[str, Any]) -> dict[str, Any]:
    try:
        with _client(base_url) as c:
            resp = c.post("/investigations", json={"profile": profile})
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach API at {base_url}: {exc}") from exc
    if resp.status_code == 422:
        raise ApiError(f"Invalid profile: {resp.json().get('detail')}")
    if resp.status_code != 200:
        raise ApiError(f"API returned HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def list_investigations(base_url: str, limit: int = 50) -> list[dict[str, Any]]:
    with _client(base_url) as c:
        resp = c.get("/investigations", params={"limit": limit})
    return resp.json() if resp.status_code == 200 else []


def submit_decision(base_url: str, task_id: str, action: str, note: str = "",
                    override_band: str | None = None) -> dict[str, Any]:
    """Human-in-the-loop: approve / override / close a paused investigation."""
    body = {"action": action, "note": note, "override_band": override_band}
    try:
        with _client(base_url) as c:
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


def get_metrics(base_url: str) -> dict[str, Any]:
    with _client(base_url) as c:
        resp = c.get("/metrics")
    return resp.json() if resp.status_code == 200 else {}
