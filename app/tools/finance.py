"""The financial-crime tools, and the default tool registry.

The analysis here is *real*, run against real inputs:

  * **Name screening** uses Jaro–Winkler similarity (the algorithm real screening
    tools use), token-aware so reordered names still match.
  * **Transaction analysis** runs on the analyst-provided ledger — structuring,
    pass-through/velocity, cash intensity, crypto and high-risk exposure are all
    computed from those actual rows.
  * **Country risk** comes from tiered FATF/Basel-style scores.

Every flag traces back to a concrete row or field, which is what makes the score
defensible rather than arbitrary.

**Nothing is ever invented.** With no ledger there is no transaction analysis —
the tools report that the evidence is absent instead of manufacturing rows to
score. What the analyst *observed* is carried separately, as attested (and
explicitly unverified) indicators, so a rating built on an attestation can never
be mistaken for one built on transactions.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.tools.datasets import (
    HIGH_RISK_COUNTERPARTIES,
    PEP_LIST,
    SANCTIONS_ENTRIES,
    STRUCTURING_THRESHOLD,
    country_risk_score,
    country_risk_tier,
    industry_risk_score,
)
from app.tools.registry import ToolRegistry, ToolSpec


# --------------------------------------------------------------------------- #
# Name matching — Jaro–Winkler (real screening algorithm)
# --------------------------------------------------------------------------- #
def _jaro(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0
    max_dist = max(len1, len2) // 2 - 1
    s1_m, s2_m = [False] * len1, [False] * len2
    matches = 0
    for i in range(len1):
        for j in range(max(0, i - max_dist), min(i + max_dist + 1, len2)):
            if not s2_m[j] and s1[i] == s2[j]:
                s1_m[i] = s2_m[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0
    t = k = 0
    for i in range(len1):
        if s1_m[i]:
            while not s2_m[k]:
                k += 1
            if s1[i] != s2[k]:
                t += 1
            k += 1
    t /= 2
    return (matches / len1 + matches / len2 + (matches - t) / matches) / 3


def _jaro_winkler(s1: str, s2: str, p: float = 0.1) -> float:
    j = _jaro(s1, s2)
    prefix = 0
    for a, b in zip(s1, s2, strict=False):
        if a != b:
            break
        prefix += 1
        if prefix == 4:
            break
    return j + prefix * p * (1 - j)


def _band(score: int) -> str:
    return ("CRITICAL" if score >= 75 else "HIGH" if score >= 50
            else "MEDIUM" if score >= 25 else "LOW")


def _norm_name(s: str) -> str:
    # Fold accents (é→e, ñ→n) instead of deleting the letter, so a diacritic in
    # a name can't push a genuine match below the screening threshold. NFKD
    # splits each letter from its combining mark; we drop the marks, then filter.
    folded = unicodedata.normalize("NFKD", (s or "").lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]", " ", folded).strip()


# How hard an unmatched token damps the score. Extra tokens are evidence of a
# different person, but not proof: middle names, patronymics and suffixes appear
# on one side of a record all the time, so the penalty is a damping factor
# rather than a disqualification.
_COVERAGE_FLOOR = 0.6


def name_match_score(a: str, b: str) -> int:
    """Token-aware fuzzy similarity in 0–100.

    Each token of the shorter name is matched to its best counterpart in the
    longer one (so reordered names still match), then the mean is damped by how
    much of the longer name went unmatched.

    Whole-string similarity alone is not safe for screening: it scores
    "Viktor Petrovich Sokolov" as a 91% match for the listed "Viktor Petrov"
    purely on the shared prefix, and finds 73% between "Alan Pritchard" and
    "Zahir Al-Rashid" on incidental letter overlap. Penalising unmatched tokens
    is what separates a longer *version* of a listed name from a different
    person who happens to share part of one.
    """
    a, b = _norm_name(a), _norm_name(b)
    if not a or not b:
        return 0
    if a == b:
        return 100
    tokens_a, tokens_b = a.split(), b.split()
    if not tokens_a or not tokens_b:
        return 0

    short, long_ = ((tokens_a, tokens_b) if len(tokens_a) <= len(tokens_b)
                    else (tokens_b, tokens_a))
    best = [max(_jaro_winkler(token, other) for other in long_) for token in short]
    mean = sum(best) / len(best)
    coverage = len(short) / len(long_)
    return round(100 * mean * (_COVERAGE_FLOOR + (1 - _COVERAGE_FLOOR) * coverage))


# --------------------------------------------------------------------------- #
# KYC tools
# --------------------------------------------------------------------------- #
def verify_identity(profile: dict[str, Any]) -> dict[str, Any]:
    """Check identity document plausibility and completeness (field by field)."""
    doc = profile.get("id_document") or {}
    checks = [
        {"field": "full_name", "label": "Full name",
         "status": "ok" if profile.get("full_name") else "missing"},
        {"field": "date_of_birth", "label": "Date of birth",
         "status": "ok" if profile.get("date_of_birth") else "missing"},
        {"field": "id_number", "label": "ID document number",
         "status": "ok" if doc.get("number") else "missing"},
        {"field": "id_expiry", "label": "ID document validity",
         "status": "invalid" if doc.get("expired") is True else "ok"},
        {"field": "source_of_funds", "label": "Declared source of funds",
         "status": "ok" if profile.get("declared_source_of_funds") else "missing"},
    ]
    issues = [c["label"].lower() for c in checks if c["status"] != "ok"]
    critical_bad = sum(1 for c in checks
                       if c["field"] in {"full_name", "date_of_birth",
                                         "id_number", "id_expiry"}
                       and c["status"] != "ok")
    soft_bad = sum(1 for c in checks
                   if c["field"] == "source_of_funds" and c["status"] != "ok")
    data_quality_score = max(0, 100 - critical_bad * 25 - soft_bad * 10)
    document_valid = critical_bad == 0
    country = profile.get("country", "")
    nationality = profile.get("nationality", "")
    tax_residency = profile.get("tax_residency", "")

    # --- customer-risk signals from the exact profile details -------------
    industry = profile.get("industry", "") or ""
    industry_risk = industry_risk_score(industry)
    remote_onboarding = str(profile.get("onboarding_channel", "in_person")).lower() \
        == "remote"
    income = float(profile.get("annual_income") or 0)
    expected_annual = float(profile.get("expected_monthly_volume") or 0) * 12
    # Declared activity far above declared income is a classic red flag.
    income_mismatch = income > 0 and expected_annual > 3 * income
    declared_pep = bool(profile.get("pep_declared"))

    return {
        "identity_verified": document_valid and data_quality_score >= 75,
        "document_valid": document_valid,
        "issues": issues,
        "checks": checks,
        "missing_fields": [c["label"] for c in checks if c["status"] != "ok"],
        "data_quality_score": data_quality_score,
        "residence_country": country,
        "residence_risk_tier": country_risk_tier(country),
        "residence_high_risk": country_risk_score(country) >= 70,
        "nationality": nationality,
        "nationality_risk": country_risk_score(nationality) if nationality else 0,
        "tax_residency": tax_residency,
        "tax_residency_risk": country_risk_score(tax_residency) if tax_residency else 0,
        "industry": industry,
        "industry_risk": industry_risk,
        "remote_onboarding": remote_onboarding,
        "income_mismatch": income_mismatch,
        "declared_pep": declared_pep,
    }


def screen_pep(full_name: str) -> dict[str, Any]:
    """Politically Exposed Person screening (fuzzy)."""
    best_role, best_score = None, 0
    for name, role in PEP_LIST.items():
        s = name_match_score(full_name, name)
        if s > best_score:
            best_role, best_score = role, s
    is_pep = best_score >= 88
    return {"is_pep": is_pep, "pep_role": best_role if is_pep else None,
            "pep_match_score": best_score}


# --------------------------------------------------------------------------- #
# Analyst-attested indicators — what was OBSERVED, when no ledger exists
# --------------------------------------------------------------------------- #
# An analyst's observation is legitimate evidence, but it is weaker than a
# transaction you can point at: it carries a lower weight than the equivalent
# verified pattern and is labelled unverified everywhere it surfaces.
ATTESTED_INDICATORS: list[dict[str, Any]] = [
    {"indicator": "structuring", "weight": 30,
     "keywords": ("structuring", "just under", "under threshold", "cash deposit",
                  "smurfing"),
     "detail": "cash deposits just under the reporting threshold"},
    {"indicator": "rapid_movement", "weight": 22,
     "keywords": ("layering", "pass-through", "passthrough", "rapid", "in and out"),
     "detail": "rapid in-and-out transfers (layering / pass-through)"},
    {"indicator": "offshore_shell", "weight": 18,
     "keywords": ("offshore", "shell compan", "shell corp"),
     "detail": "dealings with offshore or shell companies"},
    {"indicator": "high_risk_jurisdiction", "weight": 15,
     "keywords": ("high-risk jurisdiction", "high risk jurisdiction"),
     "detail": "transfers to / from high-risk jurisdictions"},
    {"indicator": "crypto_exposure", "weight": 14,
     "keywords": ("crypto", "anonymous wallet", "mixer"),
     "detail": "cryptocurrency / anonymous-wallet activity"},
    {"indicator": "large_vs_income", "weight": 14,
     "keywords": ("large transaction", "unusually large"),
     "detail": "transactions unusually large versus declared income"},
]


def attested_observations(notes: str) -> list[dict[str, Any]]:
    """Parse the analyst's observed-activity notes into structured indicators.

    These are *declarations*, not measurements: each carries ``verified: False``
    so nothing downstream can present it as transaction evidence.
    """
    text = (notes or "").lower()
    return [{"indicator": ind["indicator"], "weight": ind["weight"],
             "detail": ind["detail"], "source": "analyst-attested", "verified": False}
            for ind in ATTESTED_INDICATORS
            if any(k in text for k in ind["keywords"])]


def _no_ledger_findings(profile: dict[str, Any]) -> dict[str, Any]:
    """AML findings when there is no ledger: zeros, and why — never invented rows."""
    observed = attested_observations(profile.get("notes", ""))
    return {
        "ledger_available": False,
        "evidence": "analyst-attested" if observed else "none",
        "source": "no transaction ledger supplied",
        "unassessed_reason": "No transaction ledger was supplied, so no transaction "
                             "analysis was performed. Add the account's transactions "
                             "to assess structuring, layering, cash and crypto.",
        "transaction_count": 0, "total_volume": 0.0, "total_in": 0.0, "total_out": 0.0,
        "cash_total": 0.0, "cash_in": 0.0, "cash_out": 0.0,
        "cash_ratio": 0.0, "cash_ratio_basis": "inflow", "crypto_total": 0.0,
        "largest_transaction": 0.0,
        "structuring_detected": False, "near_threshold_count": 0,
        "near_threshold_proximity": 0.0, "crypto_ratio": 0.0,
        "high_risk_volume_share": 0.0, "high_risk_country_share": 0.0,
        "passthrough_volume_share": 0.0, "passthrough_symmetry": 0.0,
        "over_expected_ratio": 0.0,
        "rapid_movement": False, "passthrough_counterparties": [],
        "high_risk_counterparties": [], "high_risk_countries": [],
        "over_expected_volume": False, "high_velocity": False, "velocity_max_7d": 0,
        "suspicious_patterns": [],          # verified patterns only — there are none
        "attested_observations": observed,
        "aml_signal_count": 0,
        "sar_candidate": False,             # a SAR needs evidence, not an assumption
        "transactions": [], "flagged_transactions": [],
    }


def _coerce_ledger(raw: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        try:
            amt = float(t.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        out.append({
            "date": str(t.get("date") or "").strip(),
            "amount": round(amt, 2),
            "direction": "out" if str(t.get("direction", "in")).lower() == "out" else "in",
            "counterparty": str(t.get("counterparty") or "").strip(),
            "channel": str(t.get("channel") or "wire").lower(),
            "country": str(t.get("country") or "").strip(),
        })
    return out


def _velocity_7d(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    """Real time-window velocity from transaction dates.

    Returns the busiest 7-day window (count) and the ledger's date span. Only
    meaningful when the analyst supplies dated transactions; falls back to zero
    when fewer than three rows carry a parseable date.
    """
    import datetime as _dt

    dates: list[_dt.date] = []
    for t in ledger:
        raw = t.get("date") or ""
        try:
            dates.append(_dt.date.fromisoformat(raw[:10]))
        except (ValueError, TypeError):
            continue
    if len(dates) < 3:
        return {"max_in_7d": 0, "span_days": 0, "dated": len(dates)}
    dates.sort()
    max_in_7d = max(
        sum(1 for d in dates if start <= d <= start + _dt.timedelta(days=7))
        for start in dates
    )
    return {"max_in_7d": max_in_7d, "span_days": (dates[-1] - dates[0]).days,
            "dated": len(dates)}


def analyze_transactions(profile: dict[str, Any]) -> dict[str, Any]:
    """Analyse the ledger for AML typologies. Every flag maps to a real row."""
    thr = STRUCTURING_THRESHOLD
    provided = profile.get("transactions") or []
    if not provided:
        return _no_ledger_findings(profile)
    ledger = _coerce_ledger(provided)
    expected_monthly = float(profile.get("expected_monthly_volume") or 0)

    # Per-counterparty flow (for pass-through / round-trip detection).
    flow: dict[str, dict[str, float]] = {}
    for t in ledger:
        cp = t["counterparty"].lower() or "(unknown)"
        f = flow.setdefault(cp, {"in": 0.0, "out": 0.0})
        f[t["direction"]] += t["amount"]
    passthrough_cps = {cp for cp, f in flow.items()
                       if f["in"] > 0 and f["out"] > 0
                       and min(f["in"], f["out"]) >= 0.6 * max(f["in"], f["out"])
                       and max(f["in"], f["out"]) >= 2_000}

    enriched: list[dict[str, Any]] = []
    for i, t in enumerate(ledger):
        cp_l = t["counterparty"].lower()
        flags: list[str] = []
        if 0.85 * thr <= t["amount"] < thr:
            flags.append("near_threshold")
        if cp_l in HIGH_RISK_COUNTERPARTIES:
            flags.append("high_risk_counterparty")
        if t["channel"] == "cash":
            flags.append("cash")
        if t["channel"] == "crypto":
            flags.append("crypto")
        if t["country"] and country_risk_score(t["country"]) >= 70:
            flags.append("high_risk_country")
        if cp_l in passthrough_cps:
            flags.append("pass_through")
        if t["amount"] >= 12_000:
            flags.append("high_value")
        enriched.append({"id": f"txn_{i}", **t, "flags": flags})

    total = round(sum(t["amount"] for t in enriched), 2)
    total_in = round(sum(t["amount"] for t in enriched if t["direction"] == "in"), 2)
    total_out = round(sum(t["amount"] for t in enriched if t["direction"] == "out"), 2)
    cash_total = round(sum(t["amount"] for t in enriched if t["channel"] == "cash"), 2)
    cash_in = round(sum(t["amount"] for t in enriched
                        if t["channel"] == "cash" and t["direction"] == "in"), 2)
    cash_out = round(cash_total - cash_in, 2)
    crypto_total = round(sum(t["amount"] for t in enriched if t["channel"] == "crypto"), 2)

    near = [t for t in enriched if "near_threshold" in t["flags"]]
    structuring_detected = len(near) >= 3
    # How tightly the deposits hugged the limit: 8,600 and 9,950 are both "near
    # threshold", but one is far more deliberate than the other. 0 = bottom of
    # the band, 1 = right under the limit.
    near_proximity = (round(min(1.0, max(0.0,
                      (sum(t["amount"] for t in near) / len(near) / thr - 0.85) / 0.15)), 3)
                      if near else 0.0)
    rapid_movement = len(passthrough_cps) > 0
    high_risk_hits = sorted({t["counterparty"] for t in enriched
                             if "high_risk_counterparty" in t["flags"] and t["counterparty"]})
    high_risk_countries = sorted({t["country"] for t in enriched
                                  if "high_risk_country" in t["flags"] and t["country"]})
    # Cash intensity is measured against the side the cash actually moves on.
    # Dividing by gross throughput lets unrelated outflow dilute a pile of cash
    # deposits — a customer could mask cash intensity by adding ordinary payments.
    ratio_in = cash_in / total_in if total_in else 0.0
    ratio_out = cash_out / total_out if total_out else 0.0
    cash_ratio = round(max(ratio_in, ratio_out), 3)
    cash_ratio_basis = "inflow" if ratio_in >= ratio_out else "outflow"
    crypto_ratio = round(crypto_total / total, 3) if total else 0.0
    # Expected activity is monthly; scale it to the period the ledger covers so a
    # normal multi-month ledger doesn't trip the check on cumulative volume alone.
    _span_days = _velocity_7d(enriched)["span_days"]
    _months = max(1.0, _span_days / 30) if _span_days else 1.0
    over_expected = bool(expected_monthly and total_in > 3 * expected_monthly * _months)
    over_expected_ratio = (round(total_in / (expected_monthly * _months), 2)
                           if expected_monthly else 0.0)
    # Magnitudes the risk model scales on, so two accounts that both trip a
    # detector are still told apart by how badly they trip it.
    hr_volume = sum(t["amount"] for t in enriched
                    if "high_risk_counterparty" in t["flags"])
    hr_share = round(hr_volume / total, 3) if total else 0.0
    hr_country_volume = sum(t["amount"] for t in enriched
                            if "high_risk_country" in t["flags"])
    hr_country_share = round(hr_country_volume / total, 3) if total else 0.0
    pt_volume = sum(t["amount"] for t in enriched
                    if t["counterparty"].lower() in passthrough_cps)
    pt_share = round(pt_volume / total, 3) if total else 0.0
    pt_symmetry = round(max(
        (min(f["in"], f["out"]) / max(f["in"], f["out"])
         for cp, f in flow.items() if cp in passthrough_cps), default=0.0), 3)
    velocity = _velocity_7d(enriched)
    high_velocity = velocity["max_in_7d"] >= 8       # ≥8 transactions in a 7-day window
    flagged = [t for t in enriched if t["flags"]]

    patterns: list[str] = []
    if structuring_detected:
        patterns.append(f"{len(near)} deposits just under the {thr:,.0f} reporting "
                        f"threshold (structuring / smurfing)")
    if rapid_movement:
        patterns.append(f"pass-through activity: funds routed in and back out via "
                        f"{', '.join(sorted(passthrough_cps))}")
    if cash_ratio >= 0.3:
        patterns.append(f"cash-intensive: {cash_ratio:.0%} of {cash_ratio_basis} is cash")
    if crypto_total > 0:
        patterns.append(f"cryptocurrency exposure (USD {crypto_total:,.0f})")
    if high_risk_hits:
        patterns.append(f"exposure to high-risk counterparties: {', '.join(high_risk_hits)}")
    if over_expected:
        patterns.append(f"inflow USD {total_in:,.0f} far exceeds expected "
                        f"USD {expected_monthly:,.0f}/mo")
    if high_velocity:
        patterns.append(f"velocity spike: {velocity['max_in_7d']} transactions within a "
                        f"7-day window")

    signals = sum([structuring_detected, rapid_movement, cash_ratio >= 0.3,
                   crypto_total > 0, bool(high_risk_hits), over_expected, high_velocity])
    return {
        "ledger_available": True,
        "evidence": "ledger",
        "source": "analyst-provided",
        "attested_observations": attested_observations(profile.get("notes", "")),
        "transaction_count": len(enriched),
        "total_volume": total,
        "total_in": total_in,
        "total_out": total_out,
        "cash_total": cash_total,
        "cash_in": cash_in,
        "cash_out": cash_out,
        "cash_ratio": cash_ratio,
        "cash_ratio_basis": cash_ratio_basis,
        "crypto_total": crypto_total,
        "largest_transaction": max((t["amount"] for t in enriched), default=0.0),
        "structuring_detected": structuring_detected,
        "near_threshold_count": len(near),
        "near_threshold_proximity": near_proximity,
        "crypto_ratio": crypto_ratio,
        "high_risk_volume_share": hr_share,
        "high_risk_country_share": hr_country_share,
        "passthrough_volume_share": pt_share,
        "passthrough_symmetry": pt_symmetry,
        "over_expected_ratio": over_expected_ratio,
        "rapid_movement": rapid_movement,
        "passthrough_counterparties": sorted(passthrough_cps),
        "high_risk_counterparties": high_risk_hits,
        "high_risk_countries": high_risk_countries,
        "over_expected_volume": over_expected,
        "high_velocity": high_velocity,
        "velocity_max_7d": velocity["max_in_7d"],
        "suspicious_patterns": patterns,
        "aml_signal_count": signals,
        "sar_candidate": signals >= 2,
        "transactions": enriched,
        "flagged_transactions": flagged,
    }


# --------------------------------------------------------------------------- #
# Fraud detection — theft/deception typologies (distinct from AML laundering)
# --------------------------------------------------------------------------- #
def detect_fraud(profile: dict[str, Any]) -> dict[str, Any]:
    """Scan the ledger + profile for FRAUD typologies.

    Fraud ≠ AML: money-laundering hides the origin of funds (structuring,
    layering); fraud steals them (account takeover, card testing, scam/new-payee
    transfers, mule dispersal, synthetic-identity abuse). This looks for those
    theft/deception patterns — every signal traces back to real rows/fields.

    Every typology below needs transactions to look at, so with no ledger the
    honest answer is "not assessed" — not a reassuring zero.
    """
    provided = profile.get("transactions") or []
    if not provided:
        return {
            "assessed": False,
            "fraud_score": 0, "fraud_band": "NOT ASSESSED",
            "typologies": [], "signal_count": 0, "flagged_transactions": [],
            "fraud_alert": False, "summary_patterns": [],
            "unassessed_reason": "No transaction ledger was supplied — fraud "
                                 "typologies (card testing, new-payee transfers, "
                                 "mule dispersal, velocity) all require transactions.",
        }
    ledger = _coerce_ledger(provided)
    account_age = int(profile.get("account_age_days") or 0)

    count = len(ledger)
    total_in = sum(t["amount"] for t in ledger if t["direction"] == "in")
    total_out = sum(t["amount"] for t in ledger if t["direction"] == "out")
    out_txns = [t for t in ledger if t["direction"] == "out"]
    largest = max((t["amount"] for t in ledger), default=0.0)

    # counterparty frequency (a payee seen once = "new payee")
    cp_counts: dict[str, int] = {}
    for t in ledger:
        cp_counts[t["counterparty"].lower()] = cp_counts.get(t["counterparty"].lower(), 0) + 1
    distinct_out_payees = {t["counterparty"].lower() for t in out_txns if t["counterparty"]}
    countries = {t["country"] for t in ledger if t["country"]}

    typologies: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []

    def flag(txns: list[dict], label: str) -> None:
        for t in txns:
            flagged.append({**t, "fraud_flag": label})

    def _ramp(value: float, lo: float, hi: float) -> float:
        return 0.0 if hi <= lo else max(0.0, min(1.0, (value - lo) / (hi - lo)))

    # Weights scale with the magnitude of what was found, so two accounts that
    # trip the same typology are still separated by how badly they trip it.
    # 1) New account + high velocity → synthetic/first-party fraud
    if 0 < account_age < 30 and count >= 10:
        w = 20 + 15 * _ramp(count, 10, 40) + 5 * _ramp(30 - account_age, 0, 25)
        typologies.append({"type": "new_account_velocity", "weight": round(w),
                           "detail": f"{count} transactions on a {account_age}-day-old account"})

    # 2) Card testing — several small card charges (stolen-card validation)
    card_tests = [t for t in ledger if t["channel"] == "card" and t["amount"] < 100]
    if len(card_tests) >= 3:
        w = 15 + 15 * _ramp(len(card_tests), 3, 15)
        typologies.append({"type": "card_testing", "weight": round(w),
                           "detail": f"{len(card_tests)} small card charges (< USD 100)"})
        flag(card_tests, "card_testing")

    # 3) New-payee large transfer — classic APP/scam pattern
    new_payee_large = [t for t in out_txns
                       if t["amount"] >= 5_000 and cp_counts.get(t["counterparty"].lower(), 0) == 1]
    if new_payee_large:
        biggest = max(t["amount"] for t in new_payee_large)
        w = (15 + 10 * _ramp(len(new_payee_large), 1, 5)
             + 8 * _ramp(biggest, 5_000, 50_000))
        typologies.append({"type": "new_payee_large_transfer", "weight": round(w),
                           "detail": f"{len(new_payee_large)} large transfer(s) to "
                                     f"first-time payees, largest USD {biggest:,.0f}"})
        flag(new_payee_large, "new_payee_large_transfer")

    # 4) Rapid dispersal to many payees (mule cash-out)
    if len(distinct_out_payees) >= 4 and total_in > 0 and total_out >= 0.8 * total_in:
        drain = total_out / total_in
        w = 15 + 12 * _ramp(len(distinct_out_payees), 4, 15) + 6 * _ramp(drain, 0.8, 1.0)
        typologies.append({"type": "rapid_dispersal", "weight": round(w),
                           "detail": f"funds dispersed to {len(distinct_out_payees)} "
                                     f"distinct payees ({drain:.0%} of inflow out)"})

    # 5) Geographic dispersion (possible account takeover / impossible travel proxy)
    if len(countries) >= 3:
        w = 10 + 12 * _ramp(len(countries), 3, 10)
        typologies.append({"type": "geographic_dispersion", "weight": round(w),
                           "detail": f"counterparties across {len(countries)} countries"})

    # 6) Round-amount scripting (automated fraud often uses round numbers)
    round_amts = [t for t in ledger if t["amount"] >= 1_000 and t["amount"] % 1_000 == 0]
    if len(round_amts) >= 3:
        w = 6 + 10 * _ramp(len(round_amts), 3, 12)
        typologies.append({"type": "round_amount_scripting", "weight": round(w),
                           "detail": f"{len(round_amts)} exact round-thousand amounts"})

    # 7) High value on a brand-new account
    if 0 < account_age < 14 and largest >= 10_000:
        w = 12 + 14 * _ramp(largest, 10_000, 100_000)
        typologies.append({"type": "high_value_new_account", "weight": round(w),
                           "detail": f"USD {largest:,.0f} on a {account_age}-day-old "
                                     f"account"})

    # 8) Velocity spike — a burst of transactions in a short window (ATO / mule)
    velocity = _velocity_7d(ledger)
    if velocity["max_in_7d"] >= 6:
        w = 12 + 16 * _ramp(velocity["max_in_7d"], 6, 25)
        typologies.append({"type": "velocity_spike", "weight": round(w),
                           "detail": f"{velocity['max_in_7d']} transactions within a "
                                     f"7-day window"})

    fraud_score = min(100, sum(t["weight"] for t in typologies))
    band = _band(fraud_score)
    return {
        "assessed": True,
        "fraud_score": fraud_score,
        "fraud_band": band,
        "typologies": typologies,
        "signal_count": len(typologies),
        "flagged_transactions": flagged,
        "fraud_alert": fraud_score >= 50,
        "summary_patterns": [t["detail"] for t in typologies],
    }


# --------------------------------------------------------------------------- #
# Sanctions screening — fuzzy name match + blocked-country check
# --------------------------------------------------------------------------- #
def _match_entries(name: str) -> list[dict[str, Any]]:
    out = []
    for entry in SANCTIONS_ENTRIES:
        score = name_match_score(name, entry["name"])
        if score >= 70:
            out.append({"matched_name": entry["name"], "list_name": entry["list_name"],
                        "program": entry["program"],
                        "entity_type": entry.get("type", "individual"),
                        "match_score": score})
    out.sort(key=lambda m: m["match_score"], reverse=True)
    return out


def screen_sanctions(full_name: str, country: str = "",
                     counterparties: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Screen the customer AND their transaction counterparties (beneficiaries).

    Paying a sanctioned party is as serious as being one, so we fuzzy-match every
    distinct counterparty name against the list too, and flag blocked-country
    counterparties.
    """
    matches = _match_entries(full_name)
    highest = matches[0]["match_score"] if matches else 0
    if highest >= 85:
        tier, action = "STRONG", ("Block and escalate; confirm identity against the "
                                  "listed entity")
    elif highest >= 70:
        tier, action = "POSSIBLE", ("Manual review required — similarity below the "
                                    "auto-clear threshold")
    else:
        tier, action = "NONE", "No sanctions match"

    # --- counterparty (beneficiary) screening -------------------------------
    cp_matches: list[dict[str, Any]] = []
    cp_blocked: list[str] = []
    seen: set[str] = set()
    for cp in counterparties or []:
        cp_name = str(cp.get("name") or "").strip()
        cp_country = str(cp.get("country") or "").strip()
        key = cp_name.lower()
        if cp_name and key not in seen:
            seen.add(key)
            m = _match_entries(cp_name)
            if m and m[0]["match_score"] >= 85:
                cp_matches.append({"counterparty": cp_name, **m[0]})
        if cp_country and country_risk_tier(cp_country) == "BLOCKED":
            cp_blocked.append(cp_country)
    counterparty_hit = bool(cp_matches)

    return {
        "screened_name": full_name,
        "matches": matches,
        "hit": highest >= 85,
        "match_tier": tier,
        "recommended_action": action,
        "highest_match_score": highest,
        "country_risk_tier": country_risk_tier(country),
        "blocked_country_exposure": country_risk_tier(country) == "BLOCKED"
                                    or bool(cp_blocked),
        "counterparty_matches": cp_matches,
        "counterparty_hit": counterparty_hit,
        "counterparty_blocked_countries": sorted(set(cp_blocked)),
    }


# --------------------------------------------------------------------------- #
# Registry — wires each tool to the role(s) allowed to call it
# --------------------------------------------------------------------------- #
def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolSpec("verify_identity", verify_identity, frozenset({"kyc"}),
                          "Validate identity document and completeness"))
    reg.register(ToolSpec("screen_pep", screen_pep, frozenset({"kyc"}),
                          "Politically Exposed Person screening"))
    reg.register(ToolSpec("analyze_transactions", analyze_transactions, frozenset({"aml"}),
                          "Analyse the ledger for structuring, layering, cash and crypto"))
    reg.register(ToolSpec("detect_fraud", detect_fraud, frozenset({"fraud"}),
                          "Detect fraud typologies: ATO, card testing, scam/new-payee, mule"))
    reg.register(ToolSpec("screen_sanctions", screen_sanctions, frozenset({"sanctions"}),
                          "Sanctions-list name screening and blocked-country check"))
    return reg
