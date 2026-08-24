"""Reusable HTML/SVG components for the Streamlit UI.

Everything here returns a string of self-contained HTML (with inline styles) so
it renders identically regardless of Streamlit's theme quirks. Keeping the
visuals in one place keeps app.py readable.
"""

from __future__ import annotations

import html
import math

# Agent pipeline order (Orchestrator drives the six specialists).
PIPELINE = ["orchestrator", "kyc", "aml", "sanctions", "fraud", "risk", "reporting"]
LABELS = {
    "orchestrator": "Orchestrator", "kyc": "KYC", "aml": "AML",
    "sanctions": "Sanctions", "fraud": "Fraud", "risk": "Risk",
    "reporting": "Reporting",
}
ICONS = {"orchestrator": "🧭", "kyc": "🪪", "aml": "💸", "sanctions": "🚫",
         "fraud": "🎣", "risk": "📊", "reporting": "📝"}

BAND_COLORS = {"CRITICAL": "#e5484d", "HIGH": "#f76808", "MEDIUM": "#f5d90a",
               "LOW": "#30a46c"}


def band_color(band: str | None) -> str:
    return BAND_COLORS.get((band or "").upper(), "#8b8d98")


# --------------------------------------------------------------------------- #
GLOBAL_CSS = """
<style>
  .kpi-row { display:flex; gap:14px; flex-wrap:wrap; margin:8px 0 4px; }
  .kpi { flex:1; min-width:130px; background:#161a23; border:1px solid #262b36;
         border-radius:14px; padding:14px 16px; }
  .kpi .label { color:#8b93a7; font-size:12px; text-transform:uppercase;
                letter-spacing:.05em; }
  .kpi .value { color:#e6e8ee; font-size:24px; font-weight:700; margin-top:4px; }
  .pill { display:inline-block; padding:3px 10px; border-radius:999px;
          font-size:12px; font-weight:700; }
  .flow { display:flex; align-items:stretch; gap:6px; flex-wrap:wrap;
          margin:6px 0 2px; }
  .node { flex:1; min-width:118px; background:#131722; border:1px solid #262b36;
          border-radius:14px; padding:12px 10px; text-align:center;
          transition:all .2s ease; }
  .node .ico { font-size:22px; }
  .node .nm { font-weight:700; font-size:13px; margin-top:2px; color:#e6e8ee; }
  .node .st { font-size:11px; margin-top:4px; }
  .node .lat { font-size:11px; color:#8b93a7; margin-top:2px; font-family:monospace; }
  .arrow { align-self:center; color:#3a4152; font-size:18px; }
  .card { background:#131722; border:1px solid #262b36; border-radius:14px;
          padding:16px 18px; margin-bottom:12px; }
  .card h4 { margin:0 0 10px; font-size:15px; color:#e6e8ee; }
  .flag { background:#1c2130; border-left:3px solid #f76808; padding:6px 10px;
          border-radius:6px; margin:5px 0; font-size:13px; color:#cdd3e0; }
  .ok { color:#30a46c; } .bad { color:#e5484d; } .muted { color:#8b93a7; }
  table.mini { width:100%; border-collapse:collapse; font-size:13px; }
  table.mini th { text-align:left; color:#8b93a7; font-weight:600;
                  border-bottom:1px solid #262b36; padding:6px 8px; }
  table.mini td { padding:6px 8px; border-bottom:1px solid #1b2029; color:#cdd3e0; }
</style>
"""


def kpi_row(items: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<div class="kpi"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{v}</div></div>' for label, v in items)
    return f'<div class="kpi-row">{cells}</div>'


def pipeline_html(statuses: dict[str, str], latencies: dict[str, float]) -> str:
    """statuses[role] in {pending, active, done, error}."""
    style = {
        "pending": ("#131722", "#262b36", '<span class="muted">• pending</span>'),
        "active":  ("#152036", "#4c8bf5", '<span style="color:#4c8bf5">▶ working…</span>'),
        "done":    ("#122019", "#30a46c", '<span class="ok">✓ done</span>'),
        "error":   ("#231519", "#e5484d", '<span class="bad">✗ failed</span>'),
    }
    nodes = []
    for i, role in enumerate(PIPELINE):
        state = statuses.get(role, "pending")
        bg, border, badge = style[state]
        lat = latencies.get(role)
        lat_html = f'<div class="lat">{lat:.0f} ms</div>' if lat else '<div class="lat">&nbsp;</div>'
        nodes.append(
            f'<div class="node" style="background:{bg};border-color:{border}">'
            f'<div class="ico">{ICONS[role]}</div>'
            f'<div class="nm">{LABELS[role]}</div>'
            f'<div class="st">{badge}</div>{lat_html}</div>')
        if i < len(PIPELINE) - 1:
            nodes.append('<div class="arrow">▸</div>')
    return f'<div class="flow">{"".join(nodes)}</div>'


def risk_gauge(score: int, band: str) -> str:
    color = band_color(band)
    r, cx, cy = 54, 70, 70
    circ = 2 * math.pi * r
    dash = circ * max(0, min(100, score)) / 100
    return f"""
    <svg width="150" height="150" viewBox="0 0 140 140">
      <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#262b36" stroke-width="14"/>
      <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="14"
              stroke-linecap="round" stroke-dasharray="{dash:.1f} {circ:.1f}"
              transform="rotate(-90 {cx} {cy})"/>
      <text x="{cx}" y="66" text-anchor="middle" font-size="34" font-weight="800"
            fill="#e6e8ee">{score}</text>
      <text x="{cx}" y="90" text-anchor="middle" font-size="13" font-weight="800"
            fill="{color}">{html.escape(band)}</text>
    </svg>"""


def pill(text: str, color: str, bg: str | None = None) -> str:
    bg = bg or f"{color}22"
    return (f'<span class="pill" style="color:{color};background:{bg}">'
            f'{html.escape(text)}</span>')


DECISION_COLORS = {
    "DECLINE & FILE SAR": "#e5484d", "ENHANCED DUE DILIGENCE": "#f76808",
    "STANDARD DUE DILIGENCE": "#f5d90a", "APPROVE": "#30a46c",
}


def decision_banner(summary: dict) -> str:
    band = (summary.get("risk_band") or "—").upper()
    decision = summary.get("decision") or "—"
    color = DECISION_COLORS.get(decision, band_color(band))
    score = summary.get("risk_score") or 0
    conf = summary.get("confidence") or 0
    conf_label = summary.get("confidence_label") or ""
    sar = summary.get("sar_recommended")
    sar_html = ('<span class="pill" style="color:#fff;background:#e5484d">SAR REQUIRED</span>'
                if sar else '<span class="pill" style="color:#30a46c;'
                            'background:#30a46c22">No SAR</span>')
    return f"""
    <div style="display:flex;gap:18px;align-items:center;background:{color}14;
                border:1px solid {color}55;border-left:6px solid {color};
                border-radius:14px;padding:16px 20px;margin:6px 0 4px">
      <div style="flex:1">
        <div style="color:#8b93a7;font-size:12px;text-transform:uppercase;
                    letter-spacing:.06em">Recommended decision</div>
        <div style="font-size:26px;font-weight:800;color:{color};margin-top:2px">
             {html.escape(decision)}</div>
        <div style="margin-top:8px">{sar_html}
          <span class="pill" style="color:{color};background:{color}22">
            {html.escape(band)} · {score}/100</span></div>
      </div>
      <div style="width:180px">
        <div style="color:#8b93a7;font-size:12px">Confidence · {html.escape(conf_label)}</div>
        {confidence_bar(int(conf))}
      </div>
    </div>"""


def failure_banner(failed: list[dict], state: str) -> str:
    """Shown INSTEAD of the decision banner when the pipeline did not complete.

    A compliance verdict is only as good as the agents that produced it. If any
    specialist failed, the scores below are computed from missing evidence — so
    the UI must say so instead of presenting a green, low-risk-looking result.
    """
    rows = "".join(
        f'<tr><td>{ICONS.get(s.get("agent"), "")} '
        f'{html.escape(LABELS.get(s.get("agent"), str(s.get("agent"))))}</td>'
        f'<td class="bad">{html.escape(str(s.get("error") or "no result"))}</td></tr>'
        for s in failed)
    names = ", ".join(LABELS.get(s.get("agent"), str(s.get("agent"))) for s in failed)
    return f"""
    <div style="background:#231519;border:1px solid #e5484d;border-left:6px solid #e5484d;
                border-radius:14px;padding:16px 20px;margin:6px 0 10px">
      <div style="font-size:20px;font-weight:800;color:#e5484d">
        ⚠ Investigation incomplete — no verdict
      </div>
      <div style="color:#cdd3e0;margin-top:6px">
        {len(failed)} of {len(PIPELINE) - 1} agents did not return findings
        ({html.escape(names)}), so the risk score and decision below would be based
        on missing evidence and are <b>withheld</b>. Task state:
        <code>{html.escape(state)}</code>. Fix the cause and re-run before making a
        compliance decision.
      </div>
      <table class="mini" style="margin-top:10px">
        <tr><th>Agent</th><th>Reason</th></tr>{rows}</table>
    </div>"""


def transport_notice(origins: list[str]) -> str:
    """Warn that peers are being served in-process because their URL isn't live."""
    where = ", ".join(html.escape(o) for o in origins)
    return f"""
    <div style="background:#2a2016;border:1px solid #f5a524;border-radius:12px;
                padding:10px 14px;margin:6px 0">
      <b style="color:#f5a524">⚠ A2A peers served in-process</b>
      <div style="color:#cdd3e0;margin-top:3px;font-size:13px">
        Nothing is listening at {where}, so the orchestrator is calling its peers
        through the in-process transport instead of over the network. Results are
        correct, but point the <code>*_URL</code> settings at the port the API
        actually bound to restore real A2A HTTP traffic.
      </div>
    </div>"""


def confidence_bar(value: int) -> str:
    color = "#30a46c" if value >= 75 else "#f5d90a" if value >= 50 else "#f76808"
    return (f'<div style="background:#1b2029;border-radius:8px;height:16px;'
            f'overflow:hidden;margin-top:4px">'
            f'<div style="width:{max(0, min(100, value))}%;background:{color};'
            f'height:16px"></div></div>'
            f'<div style="text-align:right;font-family:monospace;font-size:12px;'
            f'color:#8b93a7;margin-top:2px">{value}/100</div>')


def identity_block(profile: dict) -> str:
    channel = "remote" if profile.get("onboarding_channel") == "remote" else "in person"
    rows = [
        ("Full name", profile.get("full_name", "—")),
        ("Date of birth", profile.get("date_of_birth") or "—"),
        ("Nationality", profile.get("nationality") or "—"),
        ("Residence", profile.get("country") or "—"),
        ("Occupation", profile.get("occupation") or "—"),
        ("Industry", profile.get("industry") or "—"),
        ("Annual income", (f"${float(profile.get('annual_income') or 0):,.0f}"
                           if profile.get("annual_income") else "—")),
        ("Source of funds", profile.get("declared_source_of_funds") or "—"),
        ("Onboarding", channel + (" · PEP" if profile.get("pep_declared") else "")),
        ("ID document", (profile.get("id_document") or {}).get("number") or "—"),
    ]
    cells = "".join(
        f'<div style="min-width:150px"><div style="color:#8b93a7;font-size:11px;'
        f'text-transform:uppercase">{html.escape(label)}</div>'
        f'<div style="color:#e6e8ee;font-weight:600;font-size:14px">'
        f'{html.escape(str(v))}</div></div>' for label, v in rows)
    return (f'<div class="card"><h4>👤 Subject</h4>'
            f'<div style="display:flex;gap:22px;flex-wrap:wrap">{cells}</div></div>')


def score_waterfall(breakdown: list[dict], band: str) -> str:
    if not breakdown:
        return '<div class="muted">No contributing factors — score 0 (LOW).</div>'
    color = band_color(band)
    rows = []
    for step in breakdown:
        name = html.escape(step["factor"].replace("_", " "))
        if not step.get("assessed", True):
            rows.append(
                f'<div style="display:flex;align-items:center;gap:10px;margin:3px 0;'
                f'opacity:.6"><div style="width:170px;font-size:13px;color:#8b93a7">'
                f'{name}</div>'
                f'<div style="width:52px;font-family:monospace;font-size:12px;'
                f'color:#8b93a7">—</div>'
                f'<div style="flex:1;font-size:12px;color:#8b93a7">'
                f'not assessed · excluded from the blend</div>'
                f'<div style="width:44px;text-align:right;font-family:monospace;'
                f'font-size:12px;color:#8b93a7">{step["running_total"]}</div></div>')
            continue
        pct = min(100, step["running_total"])
        rows.append(
            f'<div style="display:flex;align-items:center;gap:10px;margin:3px 0">'
            f'<div style="width:170px;font-size:13px;color:#cdd3e0">{name}</div>'
            f'<div style="width:52px;font-family:monospace;font-size:12px;color:#f76808">'
            f'+{step["weight"]}</div>'
            f'<div style="flex:1;background:#1b2029;border-radius:6px;overflow:hidden">'
            f'<div style="width:{pct}%;background:{color}bb;height:14px"></div></div>'
            f'<div style="width:44px;text-align:right;font-family:monospace;'
            f'font-size:12px;color:#8b93a7">{step["running_total"]}</div></div>')
    return "".join(rows)


_DIM_ORDER = [("sanctions", "Sanctions", -90), ("transaction", "Transaction", -18),
              ("fraud", "Fraud", 54), ("geographic", "Geographic", 126),
              ("customer", "Customer", 198)]


def risk_radar(dims: dict) -> str:
    """A 5-axis radar of the risk dimensions (0–100 each).

    Unassessed axes are drawn dashed and excluded from the shape: plotting them
    at zero would draw "no risk here" where the truth is "we don't know".
    """
    cx, cy, r = 140, 125, 80

    def pt(angle: float, radius: float) -> tuple[float, float]:
        a = math.radians(angle)
        return cx + radius * math.cos(a), cy + radius * math.sin(a)

    rings = ""
    for frac in (0.25, 0.5, 0.75, 1.0):
        pts = " ".join(f"{x:.1f},{y:.1f}"
                       for x, y in (pt(a, r * frac) for _, _, a in _DIM_ORDER))
        rings += f'<polygon points="{pts}" fill="none" stroke="#262b36"/>'

    axes = labels = shape = ""
    vpts = []
    for key, label, a in _DIM_ORDER:
        dim = dims.get(key, {}) or {}
        assessed = dim.get("assessed", True)
        x, y = pt(a, r)
        dash = "" if assessed else ' stroke-dasharray="3 3"'
        axes += (f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" '
                 f'stroke="#262b36"{dash}/>')
        lx, ly = pt(a, r + 15)
        fill = "#8b93a7" if assessed else "#5b6275"
        suffix = "" if assessed else " (n/a)"
        labels += (f'<text x="{lx:.1f}" y="{ly + 3:.1f}" text-anchor="middle" '
                   f'font-size="10" fill="{fill}">{label}{suffix}</text>')
        if assessed:
            v = max(0, min(100, dim.get("score", 0)))
            px, py = pt(a, r * v / 100)
            vpts.append(f"{px:.1f},{py:.1f}")

    if len(vpts) >= 3:
        shape = (f'<polygon points="{" ".join(vpts)}" fill="#4c8bf540" '
                 f'stroke="#4c8bf5" stroke-width="2"/>')
    elif vpts:  # too few axes to enclose an area — show the points instead
        shape = "".join(f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="3" '
                        f'fill="#4c8bf5"/>' for p in vpts)
    return (f'<svg width="280" height="250" viewBox="0 0 280 250">{rings}{axes}'
            f'{shape}{labels}</svg>')


def dimension_bars(dims: dict) -> str:
    """One bar per risk dimension. A dimension with no evidence reads 'not
    assessed' — never a reassuring empty bar at zero."""
    rows = []
    for key, label, _a in _DIM_ORDER:
        dim = dims.get(key, {}) or {}
        score = dim.get("score", 0)
        if not dim.get("assessed", True):
            rows.append(
                f'<div style="display:flex;align-items:center;gap:10px;margin:4px 0">'
                f'<div style="width:96px;font-size:13px;color:#8b93a7">{label}</div>'
                f'<div style="flex:1;height:14px;border-radius:6px;border:1px dashed '
                f'#3a4152;background:repeating-linear-gradient(45deg,#161a23,#161a23 5px,'
                f'#1b2029 5px,#1b2029 10px)"></div>'
                f'<div style="width:74px;text-align:right;font-size:11px;'
                f'color:#8b93a7">not assessed</div></div>')
            continue
        col = "#e5484d" if score >= 70 else "#f76808" if score >= 40 else "#30a46c"
        attested = dim.get("evidence") == "analyst-attested"
        mark = (' <span class="pill" style="color:#f5a524;background:#f5a52422;'
                'font-size:9px">ATTESTED</span>' if attested else "")
        rows.append(
            f'<div style="display:flex;align-items:center;gap:10px;margin:4px 0">'
            f'<div style="width:96px;font-size:13px;color:#cdd3e0">{label}{mark}</div>'
            f'<div style="flex:1;background:#1b2029;border-radius:6px;overflow:hidden">'
            f'<div style="width:{score}%;background:{col};height:14px"></div></div>'
            f'<div style="width:74px;text-align:right;font-family:monospace;'
            f'font-size:12px;color:#8b93a7">{score}</div></div>')
    return "".join(rows)


def coverage_note(risk: dict) -> str:
    """Say how much of the model was actually scored, and on what evidence."""
    missing = risk.get("unassessed_dimensions") or []
    conf = risk.get("confidence", {}) or {}
    coverage = risk.get("coverage_pct", 100)
    if not missing:
        return (f'<div class="muted" style="font-size:12px;margin-top:6px">'
                f'Full model coverage · {html.escape(str(conf.get("basis", "")))}</div>')
    names = ", ".join(m.replace("_", " ") for m in missing)
    return f"""
    <div style="background:#2a2016;border:1px solid #f5a524;border-radius:12px;
                padding:10px 14px;margin:8px 0">
      <b style="color:#f5a524">Provisional rating — {coverage}% model coverage</b>
      <div style="color:#cdd3e0;margin-top:3px;font-size:13px">
        <b>{html.escape(names)}</b> could not be assessed and {'was' if len(missing) == 1
        else 'were'} excluded from the blend rather than scored zero. Confidence is
        capped at {conf.get('value', 0)}/100 accordingly.
      </div>
    </div>"""


def not_assessed(title: str, reason: str, observations: list[dict] | None = None) -> str:
    """The panel shown in place of metrics when an agent had nothing to analyse."""
    obs = ""
    if observations:
        items = "".join(
            f'<li style="margin:3px 0">{html.escape(o["detail"])}</li>'
            for o in observations)
        obs = (f'<div style="margin-top:10px;color:#cdd3e0;font-size:13px">'
               f'<b style="color:#f5a524">Analyst-attested observations</b> '
               f'<span class="muted">— declared, not verified against transactions'
               f'</span><ul style="margin:6px 0 0 18px;padding:0">{items}</ul></div>')
    return f"""
    <div style="background:#161a23;border:1px dashed #3a4152;border-radius:14px;
                padding:16px 18px;margin:6px 0">
      <div style="font-weight:700;color:#e6e8ee">◌ {html.escape(title)} — not assessed</div>
      <div style="color:#8b93a7;margin-top:4px;font-size:13px">
        {html.escape(reason)}</div>
      {obs}
    </div>"""


def transactions_table(txns: list[dict], flagged_only: bool = False) -> str:
    rows_src = [t for t in txns if t.get("flags")] if flagged_only else txns
    if not rows_src:
        return '<div class="muted">No transactions to show.</div>'
    body = []
    for t in rows_src:
        flagged = bool(t.get("flags"))
        rowbg = "background:#231519" if flagged else ""
        chips = " ".join(
            f'<span class="pill" style="color:#e5484d;background:#e5484d22;'
            f'font-size:10px">{html.escape(f.replace("_", " "))}</span>'
            for f in t.get("flags", [])) or '<span class="muted">—</span>'
        body.append(
            f'<tr style="{rowbg}"><td>{html.escape(t.get("id", ""))}</td>'
            f'<td style="text-align:right;font-family:monospace">'
            f'${t.get("amount", 0):,.2f}</td>'
            f'<td>{html.escape(t.get("direction", ""))}</td>'
            f'<td>{html.escape(t.get("counterparty", ""))}</td>'
            f'<td>{chips}</td></tr>')
    return (f'<table class="mini"><tr><th>ID</th><th style="text-align:right">Amount</th>'
            f'<th>Dir</th><th>Counterparty</th><th>Flags</th></tr>'
            f'{"".join(body)}</table>')


def sanctions_table(sanc: dict) -> str:
    tier = sanc.get("match_tier", "NONE")
    tier_color = {"STRONG": "#e5484d", "POSSIBLE": "#f76808", "NONE": "#30a46c"}[tier]
    head = (f'<div style="margin-bottom:8px">{pill(f"{tier} MATCH", tier_color)} '
            f'<span class="muted">{html.escape(sanc.get("recommended_action", ""))}</span></div>')
    matches = sanc.get("matches", [])
    if not matches:
        return head + '<div class="muted">No name matches on any list.</div>'
    rows = "".join(
        f'<tr><td>{html.escape(m["matched_name"])}</td><td>{html.escape(m["list_name"])}</td>'
        f'<td>{html.escape(m["program"])}</td>'
        f'<td style="text-align:right">{m["match_score"]}%</td></tr>' for m in matches)
    return (head + f'<table class="mini"><tr><th>Matched entity</th><th>List</th>'
            f'<th>Program</th><th style="text-align:right">Score</th></tr>{rows}</table>')


def actions_checklist(actions: list[str]) -> str:
    if not actions:
        return '<div class="muted">No actions required.</div>'
    items = "".join(
        f'<div style="display:flex;gap:10px;align-items:flex-start;margin:6px 0">'
        f'<div style="color:#4c8bf5;font-size:16px">☐</div>'
        f'<div style="color:#cdd3e0;font-size:14px">{html.escape(a)}</div></div>'
        for a in actions)
    return f'<div class="card"><h4>✅ Recommended analyst actions</h4>{items}</div>'


def kyc_checks_table(checks: list[dict]) -> str:
    if not checks:
        return ""
    rows = []
    for c in checks:
        ok = c["status"] == "ok"
        mark = ('<span class="ok">✓ ok</span>' if ok
                else f'<span class="bad">✗ {html.escape(c["status"])}</span>')
        rows.append(f'<tr><td>{html.escape(c["label"])}</td><td>{mark}</td></tr>')
    return (f'<table class="mini"><tr><th>Field</th><th>Status</th></tr>'
            f'{"".join(rows)}</table>')


def trace_timeline(spans: list[dict]) -> str:
    """Horizontal latency bars for each A2A hop."""
    calls = [s for s in spans if s.get("kind") == "a2a_call"]
    if not calls:
        return '<div class="muted">no trace</div>'
    max_ms = max(s["duration_ms"] for s in calls) or 1
    rows = []
    for s in calls:
        pct = 100 * s["duration_ms"] / max_ms
        color = "#30a46c" if s["status"] == "ok" else "#e5484d"
        rows.append(
            f'<div style="display:flex;align-items:center;gap:10px;margin:4px 0">'
            f'<div style="width:90px;color:#cdd3e0;font-size:13px">{ICONS.get(s["agent"],"")} '
            f'{LABELS.get(s["agent"], s["agent"])}</div>'
            f'<div style="flex:1;background:#1b2029;border-radius:6px;overflow:hidden">'
            f'<div style="width:{pct:.0f}%;background:{color};height:16px;border-radius:6px"></div>'
            f'</div>'
            f'<div style="width:70px;text-align:right;font-family:monospace;'
            f'font-size:12px;color:#8b93a7">{s["duration_ms"]:.1f} ms</div></div>')
        # A red bar with no reason is a dead end for whoever has to fix it.
        if s.get("error"):
            rows.append(f'<div style="margin:-2px 0 6px 100px;font-size:12px" '
                        f'class="bad">↳ {html.escape(str(s["error"]))}</div>')
        elif s.get("detail", {}).get("transport") == "loopback":
            rows.append('<div style="margin:-2px 0 6px 100px;font-size:12px" '
                        'class="muted">↳ served in-process (peer URL not listening)</div>')
    return "".join(rows)
