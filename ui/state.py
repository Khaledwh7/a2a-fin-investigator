"""Session state, navigation, and the actions shared across views.

Everything here is about *who is at the console and what page they are on* —
kept in one place because the rules are subtle enough to get wrong twice
otherwise (see :data:`NAV_REQUEST`).
"""

from __future__ import annotations

import streamlit as st

from ui import api

# --- navigation ------------------------------------------------------------
# Nav labels are constants so routing compares against names, not literals, and
# the option set stays stable across runs (a changing label orphans the choice).
VIEW_INVESTIGATE = "🔎 Investigate"
VIEW_QUEUE = "⏸ Review queue"
VIEW_HISTORY = "🗂 Case history"
VIEW_METRICS = "📈 Live metrics"
VIEW_AUDIT = "🔒 Audit log"
VIEW_ABOUT = "ℹ️ About"
VIEWS = [VIEW_INVESTIGATE, VIEW_QUEUE, VIEW_HISTORY, VIEW_METRICS, VIEW_AUDIT,
         VIEW_ABOUT]

# `nav` is a widget key: Streamlit refuses writes once the radio exists, and by
# the time a row's button fires, it does. A button parks its destination here
# and the sidebar applies it on the next run, before the widget is built.
NAV_REQUEST = "_nav_request"

# The A2A state a case sits in while it waits for a human.
PENDING_STATE = "TASK_STATE_INPUT_REQUIRED"

RESULT_KEY = "last_result"


def analyst() -> str | None:
    """Whoever is at the console, for the audit trail."""
    return (st.session_state.get("analyst") or "").strip() or None


def request_view(view: str) -> None:
    """Ask for a different view on the next run (safe from inside a callback)."""
    st.session_state[NAV_REQUEST] = view


def apply_requested_view() -> None:
    """Apply a parked navigation request. Must run *before* the radio is built."""
    goto = st.session_state.pop(NAV_REQUEST, None)
    if goto is not None:
        st.session_state["nav"] = goto


def current_result() -> dict | None:
    return st.session_state.get(RESULT_KEY)


def set_result(result: dict) -> None:
    st.session_state[RESULT_KEY] = result


def clear_result() -> None:
    st.session_state.pop(RESULT_KEY, None)


# --- shared actions --------------------------------------------------------
def danger_menu(key: str, *, title: str, body: str, confirm_label: str,
                tooltip: str = "More actions") -> bool:
    """Destructive actions live behind a menu, never inline beside a primary one.

    Opening the menu is the intent; the labelled button inside it is the
    confirmation. That keeps rows uncluttered, keeps "delete" from sitting one
    pixel from "review", and gives the warning somewhere to live at full width
    instead of being squeezed into a table column.
    """
    with st.popover("⋯", help=tooltip):
        st.markdown(f"**{title}**")
        st.caption(body)
        return st.button(confirm_label, key=f"danger_{key}", type="primary",
                         width="stretch")


DELETE_WARNING = ("Removes the investigation and all six agent tasks under its "
                  "context. This cannot be undone; the deletion is recorded in "
                  "the audit log.")


def delete_case(api_url: str, task_id: str, customer: str | None) -> None:
    """Delete one investigation and drop it from the open view if it was loaded."""
    try:
        result = api.delete_investigation(api_url, task_id, analyst())
    except api.ApiError as exc:
        st.error(str(exc))
        return
    opened = current_result() or {}
    if (opened.get("task") or {}).get("id") == task_id:
        clear_result()
    st.toast(f"Deleted {customer or task_id} "
             f"({result.get('deleted', 0)} agent task(s) removed)")
    st.rerun()


def open_case(api_url: str, task_id: str) -> None:
    """Load a stored case into the Investigate view (reusing the API's summary)."""
    detail = api.get_investigation(api_url, task_id)
    if not detail:
        st.error("Could not load that investigation.")
        return
    set_result({"task": detail["task"], "trace": detail.get("trace"),
                "summary": detail.get("summary", {})})
    request_view(VIEW_INVESTIGATE)
    st.rerun()
