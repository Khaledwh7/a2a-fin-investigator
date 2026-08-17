"""Optionally run the FastAPI API *in-process* with the UI.

Single-service hosts (Streamlit Community Cloud) run one process, but the UI
talks to the API over HTTP. When ``EMBED_API`` is set (env var or a Streamlit
secret), we start the API on a background thread bound to localhost and point
the UI at it — so the whole thing deploys as one Streamlit app. Locally you
still run the two servers separately, so this stays off by default.
"""

from __future__ import annotations

import os
import threading
import time

import streamlit as st

_LOCAL_URL = "http://127.0.0.1:8000"


def _enabled() -> bool:
    if os.getenv("EMBED_API"):
        return True
    try:                                   # Streamlit secret (Cloud deploys)
        return bool(st.secrets.get("EMBED_API"))
    except Exception:
        return False


def maybe_start_embedded_api() -> str | None:
    """Start the API in-process if EMBED_API is set; return its URL (else None)."""
    if not _enabled():
        return None
    return _start_once()


@st.cache_resource(show_spinner="Starting the investigation API…")
def _start_once() -> str:
    """Start uvicorn on a daemon thread exactly once per process."""
    import httpx
    import uvicorn

    from app.api.factory import build_app
    from app.config import get_settings

    settings = get_settings()
    app = build_app(settings, database_url=settings.database_url)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8000,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()

    # Wait until it answers /healthz so the first page render finds it online.
    for _ in range(80):
        try:
            if httpx.get(f"{_LOCAL_URL}/healthz", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.25)
    return _LOCAL_URL
