"""The intake form: capturing a case and running the detection on it.

Everything the analyst types lives here — the KYC record, the transaction
ledger, and the observed-behaviour selections that feed the AML engine. The
ledger matters most: it is the only transaction evidence in the system, and
nothing is ever synthesised to fill a gap.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from collections.abc import Mapping, MutableMapping
from typing import Any

import pandas as pd
import streamlit as st

from ui import api
from ui.components import PIPELINE, kpi_row, pipeline_html
from ui.reference import (
    ACCOUNT_PURPOSE,
    BEHAVIOURS,
    CHANNELS,
    CHANNELS_ALLOWED,
    COUNTRIES,
    DIRECTIONS,
    DOC_TYPES,
    EMPLOYMENT_STATUS,
    INDUSTRIES,
    LEDGER_COLS,
    SOURCE_OF_FUNDS,
    SOURCE_OF_WEALTH,
)
from ui.results import ART_ROLE
from ui.samples import SAMPLE_CASES, mixed_typology_ledger, starter_ledger
from ui.state import analyst, set_result

FORM_DEFAULTS = {
    "f_full_name": "", "f_dob": dt.date(1990, 1, 1),
    "f_nationality": "United Kingdom", "f_country": "United Kingdom", "f_city": "",
    "f_occupation": "", "f_employer": "", "f_industry": "Salaried employment",
    "f_employment": "Employed", "f_income": 40000, "f_sof": "Salary / employment",
    "f_sow": "Employment income", "f_purpose": "Personal banking",
    "f_volume": 5000, "f_age": 180, "f_channel": "In person (branch)",
    "f_pep": False, "f_tax": "United Kingdom",
    "f_doctype": "Passport", "f_idnum": "", "f_expired": False,
    "f_behaviours": [], "f_notes": "",
}


# --------------------------------------------------------------------------- #
# Form state
# --------------------------------------------------------------------------- #
def init_form_defaults() -> None:
    for key, value in FORM_DEFAULTS.items():
        st.session_state.setdefault(key, value)


def _bump_ledger(rows: list[dict], state: MutableMapping[str, Any]) -> None:
    """Replace the ledger and force the grid to remount with the new rows."""
    state["ledger_rows"] = rows
    state["ledger_key"] = state.get("ledger_key", 0) + 1


def load_sample(name: str, state: MutableMapping[str, Any] | None = None) -> None:
    """Prefill the whole form — profile *and* ledger — from a demo case."""
    state = st.session_state if state is None else state
    case = SAMPLE_CASES[name]
    state.update({
        "f_full_name": case["full_name"],
        "f_dob": dt.date.fromisoformat(case["dob"]),
        "f_nationality": case.get("nationality", case["country"]),
        "f_country": case["country"], "f_city": case.get("city", ""),
        "f_occupation": case["occupation"], "f_employer": case.get("employer", ""),
        "f_industry": case.get("industry", "Salaried employment"),
        "f_employment": case.get("employment", "Employed"),
        "f_income": case.get("income", 40000), "f_sof": case["sof"],
        "f_sow": case.get("sow", "Employment income"),
        "f_purpose": case.get("purpose", "Personal banking"),
        "f_doctype": case["doctype"], "f_idnum": case["idnum"], "f_expired": False,
        "f_volume": case.get("volume", 25000), "f_age": case.get("age", 90),
        "f_channel": case.get("channel", "In person (branch)"),
        "f_pep": case.get("pep", False), "f_tax": case.get("tax", case["country"]),
        "f_behaviours": case["behaviours"], "f_notes": case["notes"],
    })
    # Load the case's transactions too — a sample without its ledger would leave
    # AML and Fraud unassessed and misrepresent the case it is meant to show.
    _bump_ledger(list(case.get("ledger") or starter_ledger()), state)


def _clean_country(country: str) -> str:
    return "" if country == "Other (not listed)" else country


def profile_from_form(state: Mapping[str, Any] | None = None) -> dict:
    """Assemble a CustomerProfile dict from the intake widgets.

    Observed-activity selections are translated into the keywords the AML engine
    detects, then combined with any free-text notes. ``state`` is the widget
    store — passed explicitly so this stays a plain function of its input.
    """
    ss = st.session_state if state is None else state
    note_bits = [BEHAVIOURS[b] for b in ss.get("f_behaviours", []) if b in BEHAVIOURS]
    free = (ss.get("f_notes") or "").strip()
    dob = ss.get("f_dob")
    industry = ss.get("f_industry", "")
    return {
        "full_name": (ss.get("f_full_name") or "").strip(),
        "date_of_birth": dob.isoformat() if dob else "",
        "nationality": _clean_country(ss.get("f_nationality", "")),
        "country": _clean_country(ss.get("f_country", "")),
        "city": (ss.get("f_city") or "").strip(),
        "occupation": (ss.get("f_occupation") or "").strip(),
        "employer": (ss.get("f_employer") or "").strip(),
        "industry": "" if industry == "Not provided" else industry,
        "employment_status": ss.get("f_employment", ""),
        "annual_income": float(ss.get("f_income") or 0),
        "declared_source_of_funds": ss.get("f_sof", ""),
        "source_of_wealth": ("" if ss.get("f_sow") == "Not provided"
                             else ss.get("f_sow", "")),
        "account_purpose": ss.get("f_purpose", ""),
        "expected_monthly_volume": float(ss.get("f_volume") or 0),
        "account_age_days": int(ss.get("f_age") or 0),
        "onboarding_channel": CHANNELS.get(ss.get("f_channel", ""), "in_person"),
        "pep_declared": bool(ss.get("f_pep", False)),
        "tax_residency": _clean_country(ss.get("f_tax", "")),
        "id_document": {"type": ss.get("f_doctype", "Passport"),
                        "number": (ss.get("f_idnum") or "").strip(),
                        "expired": bool(ss.get("f_expired", False))},
        "notes": " ".join(note_bits + ([free] if free else [])),
    }


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
def clean_ledger(records: list[dict]) -> list[dict]:
    """Normalise the data-editor grid into rows the API will accept.

    A half-filled row comes back from pandas with ``NaN`` in the untouched cells.
    Sent as-is that serialises to a bare ``NaN`` literal, which is not valid JSON
    and fails the whole run with "Out of range float values are not JSON
    compliant" — so every cell is coerced here, at the boundary where the grid
    stops being a DataFrame.
    """
    def text(value: object) -> str:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return ""
        return str(value).strip()

    rows: list[dict] = []
    for r in records:
        try:
            amount = float(r.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if not amount > 0 or math.isnan(amount):
            continue                       # a row with no amount is not a movement
        direction = text(r.get("direction")).lower()
        channel = text(r.get("channel")).lower()
        rows.append({
            "date": text(r.get("date")),
            "amount": amount,
            "direction": direction if direction in DIRECTIONS else "in",
            "counterparty": text(r.get("counterparty")),
            "channel": channel if channel in CHANNELS_ALLOWED else "wire",
            "country": text(r.get("country")),
        })
    return rows


def _ledger_preview(ledger: list[dict]) -> None:
    if not ledger:
        st.caption("Ledger is empty — AML and Fraud will report **not assessed**. "
                   "Any observed activity you tick below is still recorded, as an "
                   "unverified analyst attestation.")
        return
    total = sum(float(r.get("amount") or 0) for r in ledger)
    cash = sum(float(r.get("amount") or 0) for r in ledger
               if str(r.get("channel")) == "cash")
    near = sum(1 for r in ledger if 8500 <= float(r.get("amount") or 0) < 10000)
    st.markdown(kpi_row([
        ("Rows", str(len(ledger))),
        ("Total", f"${total:,.0f}"),
        ("Cash", f"${cash:,.0f}"),
        ("Near-threshold", str(near)),
    ]), unsafe_allow_html=True)


def _render_ledger_editor() -> list[dict]:
    """The editable ledger grid, with its own toolbar above it."""
    st.markdown("##### Transaction ledger")
    st.caption("Enter the account's transactions — the AML and Fraud agents analyse "
               "**these rows** for structuring, pass-through, cash intensity, crypto "
               "and fraud typologies. Nothing is ever invented: leave the ledger "
               "empty and those two dimensions are reported as *not assessed* rather "
               "than scored.")
    fill, clear, _rest = st.columns([1, 1, 4])
    if fill.button("↻ Populate sample ledger", width="stretch"):
        _bump_ledger(mixed_typology_ledger(), st.session_state)
        st.rerun()
    if clear.button("🗑 Clear ledger", width="stretch"):
        _bump_ledger(starter_ledger(), st.session_state)
        st.rerun()

    rows = st.session_state.get("ledger_rows", starter_ledger())
    edited = st.data_editor(
        pd.DataFrame(rows, columns=LEDGER_COLS),
        key=f"ledger_editor_{st.session_state.get('ledger_key', 0)}",
        num_rows="dynamic", width="stretch", hide_index=True,
        column_config={
            "date": st.column_config.TextColumn("Date", width="small"),
            "amount": st.column_config.NumberColumn("Amount (USD)", min_value=0.0,
                                                    format="$%.2f"),
            "direction": st.column_config.SelectboxColumn("Dir", options=["in", "out"],
                                                          width="small"),
            "counterparty": st.column_config.TextColumn("Counterparty"),
            "channel": st.column_config.SelectboxColumn(
                "Channel", options=sorted(CHANNELS_ALLOWED), width="small"),
            "country": st.column_config.TextColumn("CP country", width="small"),
        })
    ledger = clean_ledger(edited.to_dict("records"))
    _ledger_preview(ledger)
    return ledger


# --------------------------------------------------------------------------- #
# Form sections
# --------------------------------------------------------------------------- #
def _section_identity() -> None:
    st.markdown("##### 1 · Identity")
    c = st.columns([2, 1])
    c[0].text_input("Full legal name *", key="f_full_name", placeholder="e.g. Jane Doe")
    c[1].date_input("Date of birth", key="f_dob", min_value=dt.date(1900, 1, 1),
                    max_value=dt.date.today())
    c = st.columns(3)
    c[0].selectbox("Nationality", COUNTRIES, key="f_nationality")
    c[1].selectbox("Country of residence", COUNTRIES, key="f_country",
                   help="Higher-risk jurisdictions are flagged automatically.")
    c[2].text_input("City", key="f_city", placeholder="e.g. London")


def _section_employment() -> None:
    st.markdown("##### 2 · Employment & wealth")
    c = st.columns(3)
    c[0].text_input("Occupation", key="f_occupation",
                    placeholder="e.g. software engineer")
    c[1].text_input("Employer", key="f_employer", placeholder="e.g. Acme Ltd")
    c[2].selectbox("Industry", INDUSTRIES, key="f_industry",
                   help="Higher-risk industries raise the customer-risk score.")
    c = st.columns(3)
    c[0].selectbox("Employment status", EMPLOYMENT_STATUS, key="f_employment")
    c[1].number_input("Annual income (USD)", min_value=0, step=5000, key="f_income")
    c[2].selectbox("Source of wealth", SOURCE_OF_WEALTH, key="f_sow")


def _section_account() -> None:
    st.markdown("##### 3 · Account & onboarding")
    c = st.columns(3)
    c[0].selectbox("Declared source of funds", SOURCE_OF_FUNDS, key="f_sof")
    c[1].selectbox("Account purpose", ACCOUNT_PURPOSE, key="f_purpose")
    c[2].selectbox("Onboarding channel", list(CHANNELS.keys()), key="f_channel",
                   help="Remote (non-face-to-face) onboarding is higher risk.")
    c = st.columns(3)
    c[0].number_input("Expected monthly volume (USD)", min_value=0, step=1000,
                      key="f_volume")
    c[1].number_input("Account age (days)", min_value=0, step=30, key="f_age")
    c[2].markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    c[2].checkbox("Self-declared PEP", key="f_pep",
                  help="Politically exposed person (or a close associate).")


def _section_document() -> None:
    st.markdown("##### 4 · Identity document")
    c = st.columns(4)
    c[0].selectbox("Document type", DOC_TYPES, key="f_doctype")
    c[1].text_input("Document number", key="f_idnum", placeholder="e.g. P1234567")
    c[2].selectbox("Tax residency", COUNTRIES, key="f_tax")
    c[3].markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    c[3].checkbox("Document expired", key="f_expired")


def _section_behaviour() -> None:
    st.markdown("##### 5 · Observed activity & behaviour")
    st.caption("Tick any patterns seen on the account — these feed the AML "
               "engine directly and change the outcome.")
    st.multiselect("Observed activity", list(BEHAVIOURS.keys()), key="f_behaviours",
                   label_visibility="collapsed")
    st.text_area("Analyst notes (optional)", key="f_notes", height=70,
                 placeholder="Anything else relevant to the case…")


def render_intake(api_url: str) -> None:
    st.markdown("### New investigation")
    st.caption("Enter the subject's details and any observed activity, then run "
               "the detection. The name is screened against sanctions & PEP "
               "watchlists; country of residence and observed behaviour drive the "
               "AML and risk analysis.")

    with st.expander("⚡ Prefill from a sample case (optional)"):
        pick, load = st.columns([4, 1])
        sample = pick.selectbox("Sample case", list(SAMPLE_CASES.keys()),
                                label_visibility="collapsed")
        if load.button("Load", width="stretch"):
            load_sample(sample)
            st.rerun()

    init_form_defaults()
    ledger = _render_ledger_editor()

    with st.form("intake_form"):
        _section_identity()
        _section_employment()
        _section_account()
        _section_document()
        _section_behaviour()
        submitted = st.form_submit_button("🔍 Run detection", type="primary",
                                          width="stretch")

    if submitted:
        run_detection(api_url, ledger)


# --------------------------------------------------------------------------- #
# Running the investigation
# --------------------------------------------------------------------------- #
def run_detection(api_url: str, ledger: list[dict]) -> None:
    """Stream a live investigation, painting the pipeline as each agent answers."""
    profile = profile_from_form()
    profile["transactions"] = ledger
    if not profile["full_name"]:
        st.error("Full legal name is required to run a detection.")
        return
    if not api.health(api_url):
        st.error(f"API not reachable at {api_url}. Start it with "
                 "`uvicorn app.main:app` (or `docker compose up`).")
        return

    board, log = st.empty(), st.empty()
    statuses = {r: "pending" for r in PIPELINE}
    # Client-observed round-trip per agent, so a run in flight shows progress
    # rather than a blank node. The authoritative server-side span timings
    # replace these as soon as the finished result renders.
    latencies: dict[str, float] = {}
    started: dict[str, float] = {}
    board.markdown(pipeline_html(statuses, latencies), unsafe_allow_html=True)

    result = None
    try:
        # Live A2A event stream — each frame is the orchestrator actually
        # reaching an agent, not a replayed animation.
        for frame in api.stream_investigation(api_url, profile, analyst()):
            kind = frame.get("type")
            if kind == "status":
                meta = frame.get("meta") or {}
                agent = meta.get("agent")
                if agent in statuses:
                    statuses[agent] = "error" if meta.get("phase") == "error" else "active"
                    started.setdefault(agent, time.perf_counter())
                if frame.get("note"):
                    log.caption(frame["note"])
            elif kind == "artifact":
                role = ART_ROLE.get(frame.get("name"))
                if role and statuses.get(role) != "error":
                    statuses[role] = "done"
                    if role in started:
                        latencies[role] = (time.perf_counter() - started[role]) * 1000
                if frame.get("summary"):
                    log.caption(frame["summary"])
            elif kind == "error":
                board.empty()
                log.empty()
                st.error(frame.get("detail", "investigation failed"))
                return
            elif kind == "done":
                result = frame
            board.markdown(pipeline_html(statuses, latencies), unsafe_allow_html=True)
    except api.ApiError as exc:
        board.empty()
        log.empty()
        st.error(str(exc))
        return

    if result is None:
        board.empty()
        log.empty()
        st.error("The investigation stream ended before returning a result.")
        return
    log.empty()
    set_result(result)
    st.rerun()
