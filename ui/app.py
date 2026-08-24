"""AI Financial Investigation Assistant — Streamlit UI.

The entry point: page chrome, the sidebar, and routing. It speaks only to the
REST API (``ui/api.py``); everything it renders lives in a focused module —

    reference.py  dropdown vocabularies for the intake form
    samples.py    ready-made demo cases, each with its ledger
    state.py      session state, navigation, shared actions
    intake.py     the form, the ledger grid, running a detection
    results.py    rendering a finished or paused investigation
    views.py      one function per page
    components.py the HTML/CSS widgets (gauge, radar, pipeline, tables)

Run with:

    streamlit run ui/app.py
"""

from __future__ import annotations

import os
import sys

import streamlit as st

# Ensure the project root is importable when launched via `streamlit run ui/app.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Page config must be the first Streamlit call in the script.
st.set_page_config(page_title="A2A Financial Investigator", page_icon="🕵️",
                   layout="wide", initial_sidebar_state="expanded")

from ui import api  # noqa: E402
from ui.components import GLOBAL_CSS, pill, transport_notice  # noqa: E402
from ui.embed import maybe_start_embedded_api  # noqa: E402
from ui.state import (  # noqa: E402
    PENDING_STATE,
    VIEW_ABOUT,
    VIEW_AUDIT,
    VIEW_HISTORY,
    VIEW_INVESTIGATE,
    VIEW_METRICS,
    VIEW_QUEUE,
    VIEWS,
    apply_requested_view,
)
from ui.views import (  # noqa: E402
    view_about,
    view_audit,
    view_history,
    view_investigate,
    view_metrics,
    view_review_queue,
)

st.markdown(GLOBAL_CSS, unsafe_allow_html=True)

# One page per nav entry — routing stays a lookup instead of an if/elif ladder.
ROUTES = {
    VIEW_INVESTIGATE: view_investigate,
    VIEW_QUEUE: view_review_queue,
    VIEW_HISTORY: view_history,
    VIEW_METRICS: view_metrics,
    VIEW_AUDIT: view_audit,
    VIEW_ABOUT: view_about,
}

HEADER_HTML = (
    '<div style="background:linear-gradient(90deg,#1a2a4d,#0e1117);'
    'border:1px solid #262b36;border-radius:16px;padding:16px 22px;'
    'margin-bottom:10px"><h2 style="margin:0;color:#e6e8ee">'
    '🕵️ AI Financial Investigation Assistant</h2>'
    '<div style="color:#8b93a7">Seven specialized AI agents collaborating over '
    'the Agent2Agent (A2A) protocol — KYC · AML · Sanctions · Fraud · Risk · '
    'Reporting</div></div>'
)


def _pending_count(api_url: str) -> int:
    """How many cases are waiting on a human. Never blocks the page on failure."""
    try:
        return sum(1 for r in api.list_investigations(api_url, limit=200)
                   if r.get("state") == PENDING_STATE)
    except Exception:
        return 0


def _connection_panel(embedded_url: str | None) -> tuple[str, dict]:
    """Where the API is, who is driving, and whether both are healthy."""
    # When the API runs in-process (single-service deploy), lock the URL.
    api_url = st.text_input("API base URL", embedded_url or api.DEFAULT_API,
                            disabled=bool(embedded_url))
    st.text_input("Analyst", key="analyst", placeholder="your name",
                  help="Recorded against every run, decision and deletion "
                       "in the audit log.")
    health = api.health_detail(api_url)
    online = health.get("status") == "ok"

    badges = [pill("● API online", "#30a46c") if online
              else pill("● API offline", "#e5484d")]
    posture = health.get("security") or {}
    if posture:
        locked = sum(1 for k in ("agent_auth", "signed_cards") if posture.get(k))
        badges.append(pill("🔒 A2A secured" if locked == 2 else "🔓 A2A open",
                           "#30a46c" if locked == 2 else "#f5a524"))
    st.markdown(" ".join(badges), unsafe_allow_html=True)

    # Surface a peer-URL mismatch here rather than letting it show up as six
    # failed agents inside a run.
    if health.get("degraded_peers"):
        st.markdown(transport_notice(health["degraded_peers"]), unsafe_allow_html=True)
    return api_url, health


def sidebar(embedded_url: str | None = None) -> tuple[str, str]:
    with st.sidebar:
        st.markdown("## 🕵️ A2A Financial Investigator")
        st.caption("Multi-agent financial-crime detection over the "
                   "Agent2Agent (A2A) protocol.")
        api_url, health = _connection_panel(embedded_url)

        st.divider()
        # Apply a navigation requested by a button on the previous run — this
        # must happen before the radio is instantiated.
        apply_requested_view()
        st.radio("View", VIEWS, key="nav")

        pending = _pending_count(api_url) if health.get("status") == "ok" else 0
        if pending:
            st.markdown(pill(f"⏸ {pending} awaiting review", "#f5a524"),
                        unsafe_allow_html=True)

        st.divider()
        st.caption("⚠️ Simulation with fictional sanctions/PEP watchlists — for "
                   "demonstration only.")
    return api_url, st.session_state.get("nav", VIEW_INVESTIGATE)


def main() -> None:
    st.markdown(HEADER_HTML, unsafe_allow_html=True)
    embedded_url = maybe_start_embedded_api()   # single-service deploy (Streamlit Cloud)
    api_url, nav = sidebar(embedded_url)
    ROUTES.get(nav, view_investigate)(api_url)


if __name__ == "__main__":
    main()
