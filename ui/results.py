"""Rendering a finished (or paused) investigation.

Reads the A2A wire-format task the REST gateway returns and lays it out: the
verdict, the agent flow, the risk model, the findings and the report. The rule
that shapes this file is that an incomplete run must never be presented as a
verdict — see :func:`render_result`.
"""

from __future__ import annotations

from html import escape as esc

import streamlit as st

from ui import api
from ui.components import (
    PIPELINE,
    actions_checklist,
    coverage_note,
    decision_banner,
    dimension_bars,
    failure_banner,
    identity_block,
    kpi_row,
    kyc_checks_table,
    not_assessed,
    pipeline_html,
    risk_gauge,
    risk_radar,
    sanctions_table,
    score_waterfall,
    trace_timeline,
    transactions_table,
)
from ui.state import analyst, set_result

# Artifact name → the agent that produced it.
ART_ROLE = {"kyc_findings": "kyc", "aml_findings": "aml",
            "sanctions_findings": "sanctions", "fraud_findings": "fraud",
            "risk_assessment": "risk", "investigation_report": "reporting"}

BANDS = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]


# --------------------------------------------------------------------------- #
# Reading the wire-format task
# --------------------------------------------------------------------------- #
def artifact(task: dict, name: str) -> dict | None:
    return next((a for a in task.get("artifacts", []) if a.get("name") == name), None)


def data(task: dict, name: str) -> dict:
    art = artifact(task, name)
    if not art:
        return {}
    return next((p["data"] for p in art.get("parts", []) if "data" in p), {}) or {}


def text(task: dict, name: str) -> str:
    art = artifact(task, name)
    if not art:
        return ""
    return "\n".join(p["text"] for p in art.get("parts", []) if p.get("text"))


def subject(task: dict) -> dict:
    """The investigated subject, recovered from the orchestrator task's history."""
    for msg in task.get("history", []):
        for part in msg.get("parts", []):
            payload = part.get("data")
            if isinstance(payload, dict) and "profile" in payload:
                return payload["profile"]
    return {}


def pipeline_state(task: dict, trace: dict | None) -> tuple[dict, dict]:
    """Agent statuses + latencies, derived from artifacts (done) and trace (timing)."""
    statuses = {r: "pending" for r in PIPELINE}
    statuses["orchestrator"] = "done"
    latencies: dict[str, float] = {}
    # An artifact means that agent finished (robust across a HITL pause/resume).
    for a in task.get("artifacts", []):
        role = ART_ROLE.get(a.get("name"))
        if role:
            statuses[role] = "done"
    for s in (trace or {}).get("spans", []):
        if s.get("kind") == "a2a_call":
            if s["status"] != "ok":
                statuses[s["agent"]] = "error"
            latencies[s["agent"]] = s["duration_ms"]
    return statuses, latencies


def failed_spans(trace: dict | None) -> list[dict]:
    return [s for s in (trace or {}).get("spans", [])
            if s.get("kind") == "a2a_call" and s.get("status") != "ok"]


# --------------------------------------------------------------------------- #
# Small presentation helpers
# --------------------------------------------------------------------------- #
def _kv(label: str, value: str, good: bool | None = None) -> None:
    cls = "" if good is None else ("ok" if good else "bad")
    st.markdown(f'<div style="margin:4px 0"><span class="muted">{esc(label)}:</span> '
                f'<b class="{cls}">{esc(str(value))}</b></div>', unsafe_allow_html=True)


def _flags(items: list[str]) -> None:
    """Render finding text. Always escaped: these strings embed analyst input
    (counterparty names, country, industry), so they are untrusted content."""
    if not items:
        st.markdown('<span class="muted">No flags.</span>', unsafe_allow_html=True)
    for it in items:
        st.markdown(f'<div class="flag">{esc(str(it))}</div>', unsafe_allow_html=True)


def _mini_table(headers: list[str], rows: str) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    return f'<table class="mini"><tr>{head}</tr>{rows}</table>'


def _escalation_note(risk: dict) -> None:
    escalations = risk.get("escalations", [])
    if escalations:
        st.caption("⭑ Escalations applied: " + "; ".join(escalations))


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def render_metrics_row(summary: dict, task: dict | None = None) -> None:
    """Run telemetry. The LLM tiles only appear when an LLM actually ran —
    a permanent '0 tokens / $0.0000' pair is noise, not information."""
    lat = summary.get("latency_ms") or 0
    aml = data(task or {}, "aml_findings")
    evidence = (f"{aml.get('transaction_count', 0)} txns"
                if aml.get("ledger_available") else "no ledger")
    tiles = [
        ("Latency", f"{lat:.0f} ms"),
        ("A2A calls", str(summary.get("agent_calls") or 0)),
        ("Evidence", evidence),
        ("Payload tokens", f"{summary.get('est_tokens') or 0:,}"),
    ]
    if summary.get("llm_tokens"):
        tiles += [("LLM tokens", f"{summary['llm_tokens']:,}"),
                  ("LLM cost", f"${summary.get('cost_usd') or 0:.4f}")]
    else:
        tiles.append(("Analysis", "deterministic"))
    st.markdown(kpi_row(tiles), unsafe_allow_html=True)


def _tab_kyc(kyc: dict) -> None:
    c1, c2 = st.columns([1, 1])
    with c1:
        _kv("Identity verified", "✓ yes" if kyc.get("identity_verified") else "✗ no",
            good=kyc.get("identity_verified"))
        _kv("Data quality", f"{kyc.get('data_quality_score', 0)}/100")
        _kv("PEP", ("yes — " + str(kyc.get("pep_role"))) if kyc.get("is_pep") else "no",
            good=not kyc.get("is_pep"))
        _flags(kyc.get("risk_flags", []))
    with c2:
        st.markdown(kyc_checks_table(kyc.get("checks", [])), unsafe_allow_html=True)


def _tab_aml(aml: dict) -> None:
    # No ledger ⇒ no metrics. Showing "0 transactions / $0 / 0 flagged" would
    # read as a clean account rather than an unexamined one.
    if not aml.get("ledger_available", True):
        st.markdown(not_assessed(
            "AML transaction monitoring",
            aml.get("unassessed_reason", "No transaction ledger was supplied."),
            aml.get("attested_observations")), unsafe_allow_html=True)
        return
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Transactions", aml.get("transaction_count", 0))
    m2.metric("Total volume", f"${aml.get('total_volume', 0):,.0f}")
    m3.metric("Near-threshold", aml.get("near_threshold_count", 0))
    m4.metric("Flagged", len(aml.get("flagged_transactions", [])))
    _flags(aml.get("suspicious_patterns", []))
    flagged_only = st.checkbox("Show flagged transactions only",
                               value=bool(aml.get("flagged_transactions")))
    st.markdown(transactions_table(aml.get("transactions", []), flagged_only),
                unsafe_allow_html=True)


def _tab_sanctions(sanc: dict) -> None:
    st.markdown(sanctions_table(sanc), unsafe_allow_html=True)
    cp_hits = sanc.get("counterparty_matches", [])
    if cp_hits:
        st.markdown('<div class="flag" style="border-color:#e5484d">'
                    '<b class="bad">BENEFICIARY SANCTIONS HIT</b> — this customer '
                    'pays a sanctioned party</div>', unsafe_allow_html=True)
        rows = "".join(
            f"<tr><td>{esc(str(m['counterparty']))}</td>"
            f"<td>{esc(str(m['matched_name']))}</td>"
            f"<td>{esc(str(m['program']))}</td>"
            f"<td style='text-align:right'>{int(m['match_score'])}%</td></tr>"
            for m in cp_hits)
        st.markdown(_mini_table(["Beneficiary", "Matched entity", "Program", "Score"],
                                rows), unsafe_allow_html=True)
    _kv("Blocked-country exposure",
        "yes" if sanc.get("blocked_country_exposure") else "no",
        good=not sanc.get("blocked_country_exposure"))


def _tab_fraud(fraud: dict) -> None:
    """Fraud findings, or an honest 'not assessed' when there was no ledger."""
    if not fraud.get("assessed", True):
        st.markdown(not_assessed(
            "Fraud typology screening",
            fraud.get("unassessed_reason", "No transaction ledger was supplied.")),
            unsafe_allow_html=True)
        return

    f1, f2 = st.columns([1, 2])
    f1.metric("Fraud score", f"{fraud.get('fraud_score', 0)}/100",
              fraud.get("fraud_band", "LOW"))
    with f2:
        if fraud.get("fraud_alert"):
            st.markdown('<div class="flag" style="border-color:#e5484d">'
                        '<b class="bad">FRAUD ALERT</b></div>',
                        unsafe_allow_html=True)
        typologies = fraud.get("typologies", [])
        if not typologies:
            st.markdown('<span class="muted">No fraud typologies detected.</span>',
                        unsafe_allow_html=True)
            return
        rows = "".join(
            f"<tr><td>{esc(str(t['type']).replace('_', ' '))}</td>"
            f"<td>+{int(t['weight'])}</td>"
            f"<td>{esc(str(t['detail']))}</td></tr>" for t in typologies)
        st.markdown(_mini_table(["Typology", "Weight", "Detail"], rows),
                    unsafe_allow_html=True)


def render_findings(task: dict) -> None:
    t1, t2, t3, t4 = st.tabs(["🪪 KYC identity", "💸 AML transactions",
                              "🚫 Sanctions", "🎣 Fraud"])
    with t1:
        _tab_kyc(data(task, "kyc_findings"))
    with t2:
        _tab_aml(data(task, "aml_findings"))
    with t3:
        _tab_sanctions(data(task, "sanctions_findings"))
    with t4:
        _tab_fraud(data(task, "fraud_findings"))


def _render_risk_model(risk: dict) -> None:
    """The gauge, radar and waterfall — how the rating was arrived at."""
    st.markdown("##### Risk assessment")
    st.markdown(coverage_note(risk), unsafe_allow_html=True)
    dims = risk.get("dimensions", {})
    gauge_col, radar_col, bars_col = st.columns([1, 1.1, 1.6])
    with gauge_col:
        st.markdown(risk_gauge(int(risk.get("risk_score", 0)),
                               risk.get("risk_band", "LOW")), unsafe_allow_html=True)
        conf = risk.get("confidence", {})
        st.caption(f"Confidence: {conf.get('value', 0)}/100 ({conf.get('label', '—')})")
        st.caption(conf.get("basis", ""))
    with radar_col:
        st.markdown(risk_radar(dims), unsafe_allow_html=True)
    with bars_col:
        st.markdown("**Risk dimensions**")
        st.markdown(dimension_bars(dims), unsafe_allow_html=True)
    st.markdown("**How the overall score was computed** (weighted blend of the five "
                "risk dimensions; escalation floors may raise it)")
    st.markdown(score_waterfall(risk.get("score_breakdown", []),
                                risk.get("risk_band", "LOW")), unsafe_allow_html=True)
    _escalation_note(risk)


def _render_report(task: dict, trace: dict | None) -> None:
    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown("##### Investigation report")
        report_md = text(task, "investigation_report") or "_no report_"
        # Streamlit renders $…$ as LaTeX math; escape so currency shows literally.
        st.markdown(report_md.replace("$", "\\$"))
        st.download_button("⬇ Download report (.md)",
                           text(task, "investigation_report"),
                           file_name=f"report_{task.get('id', 'case')}.md")
    with c2:
        st.markdown("##### Execution trace")
        st.markdown(trace_timeline((trace or {}).get("spans", [])),
                    unsafe_allow_html=True)
        with st.expander("Raw task JSON (A2A)"):
            st.json(task)


def render_result(result: dict) -> None:
    task, trace, summary = result["task"], result.get("trace"), result["summary"]
    risk = data(task, "risk_assessment")
    failed = failed_spans(trace)

    # 1) Headline. A run that lost agents has no verdict to show — presenting the
    #    partial score as a decision is the one failure mode a compliance tool
    #    must never have, so the banner is replaced by the reason it broke.
    if failed:
        st.markdown(failure_banner(failed, summary.get("state", "—")),
                    unsafe_allow_html=True)
    else:
        st.markdown(decision_banner(summary), unsafe_allow_html=True)

    # 2) Subject identity + live A2A flow.
    st.markdown(identity_block(subject(task)), unsafe_allow_html=True)
    st.markdown("##### A2A communication flow")
    st.markdown(pipeline_html(*pipeline_state(task, trace)), unsafe_allow_html=True)
    render_metrics_row(summary, task)

    if failed:
        # Findings from the agents that DID answer stay visible for diagnosis,
        # but the score, radar and report are suppressed — they are incomplete.
        st.markdown("##### Partial findings (incomplete — not a verdict)")
        render_findings(task)
        st.markdown("##### Execution trace")
        st.markdown(trace_timeline((trace or {}).get("spans", [])),
                    unsafe_allow_html=True)
        with st.expander("Raw task JSON (A2A)"):
            st.json(task)
        return

    _render_risk_model(risk)

    st.markdown("##### Findings")
    render_findings(task)

    st.markdown(actions_checklist(summary.get("recommended_actions", [])),
                unsafe_allow_html=True)

    _render_report(task, trace)


# --------------------------------------------------------------------------- #
# Human-in-the-loop review
# --------------------------------------------------------------------------- #
def _decision_form(api_url: str, task: dict, current_band: str) -> None:
    """Approve / override / close.

    The override target is chosen *before* the buttons and defaults to the band
    the model actually assigned, and the button says where it will land. An
    override control that sits below its own trigger and defaults to LOW turns
    one stray click on a CRITICAL case into a silent downgrade — which is the
    only mistake in this panel that a bank could not walk back.
    """
    st.markdown("##### Your decision")
    note = st.text_input("Rationale (recorded in the audit log)",
                         key="decision_note",
                         placeholder="e.g. confirmed sanctions match with a "
                                     "secondary source")

    index = BANDS.index(current_band) if current_band in BANDS else BANDS.index("LOW")
    band = st.selectbox("If overriding, set the band to", BANDS, index=index,
                        key="override_band",
                        help="Only used by Override. Defaults to the band the "
                             "model assigned, so an override is always a "
                             "deliberate change.")

    approve, override, close = st.columns(3)
    action: str | None = None
    if approve.button(f"✅ Approve & file ({current_band})", type="primary",
                      width="stretch"):
        action = "approve"
    if override.button(f"✏️ Override to {band}", width="stretch",
                       disabled=band == current_band,
                       help=("Choose a different band above to enable an override"
                             if band == current_band else
                             f"Re-rate this case as {band}")):
        action = "override"
    if close.button("⛔ Close (false positive)", width="stretch"):
        action = "close"
    if not action:
        return

    try:
        with st.spinner(f"Recording analyst decision ({action})…"):
            final = api.submit_decision(api_url, task["id"], action, note,
                                        band if action == "override" else None,
                                        analyst=analyst())
    except api.ApiError as exc:
        st.error(str(exc))
        return
    set_result(final)
    st.rerun()


def render_review(api_url: str, result: dict) -> None:
    """The human-in-the-loop panel: an analyst approves / overrides / closes."""
    task, summary = result["task"], result["summary"]
    st.markdown(
        '<div style="background:#2a2016;border:1px solid #f5a524;border-radius:14px;'
        'padding:14px 18px;margin-bottom:10px"><b style="color:#f5a524">⏸ Awaiting '
        'your review</b><div style="color:#cdd3e0;margin-top:4px">This case reached '
        'a high-stakes outcome and was paused at the A2A <code>INPUT_REQUIRED</code> '
        'state. Review the recommendation and decide before it is filed.</div></div>',
        unsafe_allow_html=True)

    st.markdown(decision_banner(summary), unsafe_allow_html=True)
    # The analyst is about to put their name to this rating, so the limits of the
    # evidence behind it belong here, not three sections further down.
    risk = data(task, "risk_assessment")
    st.markdown(coverage_note(risk), unsafe_allow_html=True)
    st.markdown(identity_block(subject(task)), unsafe_allow_html=True)

    st.markdown("##### Evidence behind this rating")
    ev1, ev2 = st.columns([1.6, 1])
    with ev1:
        st.markdown(dimension_bars(risk.get("dimensions", {})), unsafe_allow_html=True)
    with ev2:
        conf = risk.get("confidence", {})
        st.metric("Confidence", f"{conf.get('value', 0)}/100", conf.get("label", "—"))
        st.caption(conf.get("basis", ""))
    _escalation_note(risk)

    st.markdown("##### Findings")
    render_findings(task)

    _decision_form(api_url, task, risk.get("risk_band") or
                   summary.get("risk_band") or "LOW")
