"""The pages behind the sidebar: investigate, queue, history, metrics, audit, about.

Each ``view_*`` function owns one page and nothing else, so routing in
``ui/app.py`` stays a flat dispatch table.
"""

from __future__ import annotations

from html import escape as esc

import pandas as pd
import streamlit as st

from ui import api
from ui.components import band_color, kpi_row, pill
from ui.intake import render_intake
from ui.results import render_result, render_review
from ui.state import (
    DELETE_WARNING,
    PENDING_STATE,
    analyst,
    clear_result,
    current_result,
    danger_menu,
    delete_case,
    open_case,
)

_STATE_LABEL = {
    "TASK_STATE_INPUT_REQUIRED": "⏸ awaiting review",
    "TASK_STATE_FAILED": "✗ incomplete",
    "TASK_STATE_CANCELED": "canceled",
    "TASK_STATE_COMPLETED": "✓ complete",
}


# --------------------------------------------------------------------------- #
# Investigate
# --------------------------------------------------------------------------- #
def view_investigate(api_url: str) -> None:
    result = current_result()
    if result is None:
        render_intake(api_url)
        return

    # A finished case is showing; offer the way back to a fresh intake.
    new, _rest = st.columns([1, 4])
    if new.button("＋ New investigation", width="stretch"):
        clear_result()
        st.rerun()
    if result.get("summary", {}).get("pending_review"):
        render_review(api_url, result)     # human-in-the-loop gate
    else:
        render_result(result)


# --------------------------------------------------------------------------- #
# Review queue
# --------------------------------------------------------------------------- #
def view_review_queue(api_url: str) -> None:
    """The analyst's work list: every case paused at the human-review gate.

    These are the cases the pipeline deliberately refused to auto-file, so they
    are the only ones that actually require a person. Finding them by scrolling
    the full history is how a queue gets missed.
    """
    st.markdown("### Review queue")
    rows = [r for r in api.list_investigations(api_url, limit=200)
            if r.get("state") == PENDING_STATE]
    if not rows:
        st.success("Nothing awaiting review — every investigation has been actioned.")
        st.caption("High-risk cases (HIGH/CRITICAL, SAR recommended, or a sanctions "
                   "hit) pause here for a decision before any report is filed.")
        return

    st.caption(f"**{len(rows)}** case(s) paused at the A2A `INPUT_REQUIRED` state, "
               "awaiting your approval, override, or dismissal.")
    for r in rows:
        band = r.get("risk_band")
        score = r.get("risk_score")
        customer = r.get("customer") or "—"
        with st.container(border=True):
            # Identity and rating read left to right; the one action you are here
            # to take sits on the right, with anything destructive tucked behind it.
            info, action, menu = st.columns([6, 2, 1], vertical_alignment="center")
            info.markdown(
                f'<div style="font-weight:700;font-size:15px">{esc(str(customer))}</div>'
                f'<div style="margin-top:4px">'
                f'{pill(band, band_color(band)) if band else ""}'
                f'<span class="muted" style="margin-left:8px">'
                f'score {esc(str(score)) if score is not None else "—"}</span></div>',
                unsafe_allow_html=True)
            if action.button("Review →", key=f"review_{r['task_id']}",
                             type="primary", width="stretch"):
                open_case(api_url, r["task_id"])
            with menu:
                if danger_menu(f"q_{r['task_id']}", title=f"Delete {customer}",
                               body=DELETE_WARNING,
                               confirm_label="Delete permanently"):
                    delete_case(api_url, r["task_id"], r.get("customer"))


# --------------------------------------------------------------------------- #
# Case history
# --------------------------------------------------------------------------- #
def view_history(api_url: str) -> None:
    """Every investigation, as a table you can sort and scan.

    A browse view of dozens of rows is a table, not dozens of button pairs: the
    row carries data only, and the actions sit in one toolbar underneath that
    operates on the selected case.
    """
    st.markdown("### Case history")
    rows = api.list_investigations(api_url, limit=200)
    if not rows:
        st.info("No investigations yet, or the API is running without a database.")
        return

    pending = sum(1 for r in rows if r.get("state") == PENDING_STATE)
    st.caption(f"{len(rows)} case(s)"
               + (f" · **{pending} awaiting your review**" if pending else ""))

    table = pd.DataFrame([{
        "Customer": r.get("customer") or "—",
        "Risk band": r.get("risk_band") or "—",
        "Score": r.get("risk_score"),
        "Status": _STATE_LABEL.get(r.get("state", ""), "complete"),
        "Reference": (r.get("task_id") or "")[-8:],
    } for r in rows])

    event = st.dataframe(
        table, hide_index=True, width="stretch", height=380,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Customer": st.column_config.TextColumn(width="large"),
            "Score": st.column_config.NumberColumn(format="%d", width="small"),
            "Reference": st.column_config.TextColumn(
                width="small", help="Last 8 characters of the A2A task id"),
        })

    picked = list(event.selection.rows)
    if not picked:
        st.caption("Select a row to open or delete that case.")
        return

    case = rows[picked[0]]
    customer = case.get("customer") or case["task_id"]
    open_col, _spacer, menu_col = st.columns([2, 6, 1], vertical_alignment="center")
    if open_col.button("Open case", type="primary", width="stretch"):
        open_case(api_url, case["task_id"])
    with menu_col:
        if danger_menu(f"h_{case['task_id']}", title=f"Delete {customer}",
                       body=DELETE_WARNING, confirm_label="Delete permanently"):
            delete_case(api_url, case["task_id"], case.get("customer"))


# --------------------------------------------------------------------------- #
# Live metrics
# --------------------------------------------------------------------------- #
def view_metrics(api_url: str) -> None:
    st.markdown("### Live metrics")
    m = api.get_metrics(api_url)
    counters = m.get("counters", {})
    c = st.columns(4)
    c[0].metric("Investigations", counters.get("investigations_total", 0))
    c[1].metric("A2A calls", counters.get("agent_calls_total", 0))
    c[2].metric("Errors", counters.get("errors_total", 0))
    lat = m.get("latency", {}).get("investigation_latency_ms", {})
    c[3].metric("Avg latency", f"{lat.get('avg_ms', 0):.0f} ms")
    if lat:
        st.markdown("#### Investigation latency")
        st.markdown(kpi_row([("count", str(lat.get("count", 0))),
                             ("avg", f"{lat.get('avg_ms', 0):.0f} ms"),
                             ("p50", f"{lat.get('p50_ms', 0):.0f} ms"),
                             ("p95", f"{lat.get('p95_ms', 0):.0f} ms")]),
                    unsafe_allow_html=True)
    with st.expander("Raw /metrics"):
        st.json(m)


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
def view_audit(api_url: str) -> None:
    """The hash-chained audit trail, as a table with the chain status on top."""
    st.markdown("### Audit log")
    a = api.get_audit(api_url, limit=200)
    if not a:
        st.info("Audit log is empty, or the API is unreachable.")
        return

    # Status and the only destructive control share one header line, so the log
    # itself starts immediately below instead of being pushed down by a panel.
    valid = a.get("chain_valid")
    status, menu = st.columns([9, 1], vertical_alignment="center")
    status.markdown(
        (pill("✓ chain verified — tamper-evident", "#30a46c") if valid
         else pill("✗ chain broken — tampering detected", "#e5484d"))
        + f' &nbsp; <span class="muted">{a.get("count", 0)} entries</span>',
        unsafe_allow_html=True)
    with menu:
        # No per-entry delete: removing one row and re-hashing the rest is the
        # forgery this chain exists to expose, so the only honest removal is all
        # of it — which starts a new chain rather than faking an unbroken one.
        if danger_menu("audit_log", title="Clear the audit log",
                       body="Individual entries cannot be deleted — the log is "
                            "append-only and hash-chained. Clearing discards the "
                            "whole history and opens a fresh chain, with the "
                            "clearance written as its first entry.",
                       confirm_label=f"Delete all {a.get('count', 0)} entries",
                       tooltip="Audit log actions"):
            try:
                result = api.clear_audit(api_url, analyst())
            except api.ApiError as exc:
                st.error(str(exc))
            else:
                st.toast(f"Cleared {result.get('cleared', 0)} audit entries")
                st.rerun()

    st.caption("Every security-relevant event — auth allow/deny, investigations, "
               "prompt-injection flags, analyst decisions and deletions — is "
               "appended here. Editing any past entry breaks the chain.")

    entries = list(reversed(a.get("entries", [])))  # newest first
    if not entries:
        st.info("No entries yet.")
        return
    st.dataframe(
        pd.DataFrame([{
            "#": e["seq"],
            "Time": (e["ts"] or "")[11:19],
            "Actor": e["actor"],
            "Action": e["action"],
            "Resource": e["resource"],
            "Outcome": e["outcome"],
            "Hash": e["hash"],
        } for e in entries]),
        hide_index=True, width="stretch", height=420,
        column_config={
            "#": st.column_config.NumberColumn(width="small"),
            "Time": st.column_config.TextColumn(width="small"),
            "Action": st.column_config.TextColumn(width="medium"),
            "Resource": st.column_config.TextColumn(width="medium"),
            "Hash": st.column_config.TextColumn(
                width="small", help="First 12 characters of the entry hash"),
        })


# --------------------------------------------------------------------------- #
# About
# --------------------------------------------------------------------------- #
_A2A_FEATURES = [
    "Agent Card + discovery (`/.well-known/agent-card.json`)",
    "JSON-RPC methods (`SendMessage`, `GetTask`, `CancelTask`)",
    "Messages / Parts (text + structured data)",
    "Tasks & lifecycle (SUBMITTED→WORKING→COMPLETED/FAILED)",
    "Artifacts (each agent's findings)",
    "Shared Context (one `contextId` across all agents)",
    "Streaming (SSE) + version negotiation (`A2A-Version`)",
    "Human-in-the-loop review (A2A `INPUT_REQUIRED`)",
]

# Controls that can be switched off are reported from the running API rather
# than asserted, so this list can never claim a protection the app is not
# actually applying.
_SECURITY_TOGGLES = [
    ("JWT auth + RBAC least-privilege (mutual agent auth)", "agent_auth"),
    ("Signed Agent Cards (rogue-agent defense)", "signed_cards"),
    ("Rate limiting (per-client token bucket)", "rate_limit"),
    ("Human-in-the-loop gate on high-stakes cases", "human_review"),
]

_ALWAYS_ON = [
    "Input validation · prompt-injection guard · secret redaction",
    "Hash-chained audit log (tamper-evident)",
    "Per-agent trace: latency · tokens · cost · errors",
    "Timeouts · retries+backoff · loop/iteration caps",
    "Evaluation: 17 labelled scenarios + per-detector precision/recall",
]


def view_about(api_url: str) -> None:
    st.markdown("### What this demonstrates")
    left, right = st.columns(2)
    with left:
        st.markdown("#### A2A protocol (v1.0)")
        for item in _A2A_FEATURES:
            st.markdown(f"- ✅ {item}")
    with right:
        st.markdown("#### Security · Observability · Reliability")
        posture = (api.health_detail(api_url) or {}).get("security", {})
        for label, key in _SECURITY_TOGGLES:
            state = posture.get(key)
            mark = "✅" if state else ("⚪" if state is False else "—")
            suffix = "" if state else " _(off in this configuration)_"
            st.markdown(f"- {mark} {label}{suffix}")
        for item in _ALWAYS_ON:
            st.markdown(f"- ✅ {item}")
    st.markdown("---")
    st.markdown(
        "**Architecture:** `User → Streamlit UI → REST gateway → Orchestrator "
        "→ (A2A) KYC · AML · Sanctions · Fraud · Risk → Reporting → Report`. "
        "The UI only ever calls the REST API; agents talk to each other over "
        "real A2A JSON-RPC.")
