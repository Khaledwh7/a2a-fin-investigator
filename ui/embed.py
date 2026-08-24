"""Optionally run the FastAPI API *in-process* with the UI.

Single-service hosts (Streamlit Community Cloud) run one process, but the UI
talks to the API over HTTP. When ``EMBED_API`` is set (env var or a Streamlit
secret), we start the API on a background thread bound to localhost and point
the UI at it — so the whole thing deploys as one Streamlit app. Locally you
still run the two servers separately, so this stays off by default.

The port is claimed by *binding a socket first* and handing that socket to
uvicorn: a fixed port can already be taken on a shared host, and starting there
would either fail to bind or — worse — leave the UI and the agents' peer URLs
pointing at somebody else's service. The bound port is then written back into
the agents' peer URLs so A2A traffic reaches this app and not port 8000.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import streamlit as st


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


def _bind_socket() -> tuple[socket.socket, int]:
    """Claim a port. Honour EMBED_API_PORT if given, else let the OS pick a free one."""
    requested = int(os.getenv("EMBED_API_PORT", "0") or 0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", requested))
    except OSError:                        # requested port taken → fall back to any
        sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock, sock.getsockname()[1]


@st.cache_resource(show_spinner="Starting the investigation API…")
def _start_once() -> str:
    """Start uvicorn on a daemon thread exactly once per process."""
    import httpx
    import uvicorn

    from app.api.factory import build_app
    from app.config import AgentRole, get_settings

    sock, port = _bind_socket()
    base_url = f"http://127.0.0.1:{port}"

    # Point every agent's peer URL at the port we actually bound, so the
    # orchestrator's A2A calls reach this app.
    settings = get_settings().model_copy(update={
        "public_base_url": base_url,
        **{f"{role.value}_url": f"{base_url}/a2a/{role.value}" for role in AgentRole},
    })

    app = build_app(settings, database_url=settings.database_url)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True).start()

    # Wait until it answers /healthz so the first page render finds it online.
    for _ in range(80):
        try:
            if httpx.get(f"{base_url}/healthz", timeout=1).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.25)                    # back off on any not-ready outcome
    return base_url
