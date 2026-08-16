"""The financial-crime tools, and the default tool registry.

The analysis here is *real*, run against real inputs:

  * **Name screening** uses Jaro–Winkler similarity (the algorithm real screening
    tools use), token-aware so reordered names still match.
  * **Transaction analysis** runs on the analyst-provided ledger when present —
    structuring, pass-through/velocity, cash intensity, crypto and high-risk
    exposure are all computed from those actual rows. Only when no ledger is
    supplied do we synthesize a plausible one (kept deterministic for tests).
  * **Country risk** comes from tiered FATF/Basel-style scores.

Every flag traces back to a concrete row or field, which is what makes the score
defensible rather than arbitrary.
"""

from __future__ import annotations

import hashlib
import random
import re
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
    return re.sub(r"[^a-z ]", " ", (s or "").lower()).strip()


def name_match_score(a: str, b: str) -> int:
    """Token-aware fuzzy similarity in 0–100 (handles reordered names)."""
    a, b = _norm_name(a), _norm_name(b)
    if not a or not b:
        return 0
    direct = _jaro_winkler(a, b)
    sorted_jw = _jaro_winkler(" ".join(sorted(a.split())), " ".join(sorted(b.split())))
    return round(max(direct, sorted_jw) * 100)


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
# AML — analysis of the ACTUAL ledger (with a synthetic fallback)
# --------------------------------------------------------------------------- #
def _seed(name: str) -> int:
    return int(hashlib.sha256(name.encode()).hexdigest(), 16) % (2**32)


def _synthesize_ledger(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a structured, explainable ledger when the analyst supplies none.

    Legitimate activity always present; risky patterns appear only when the
    profile's notes indicate them — so a clean customer stays clean.
    """
    rng = random.Random(_seed(profile.get("full_name", "anon")))  # noqa: S311
    notes = (profile.get("notes") or "").lower()
    thr = STRUCTURING_THRESHOLD

    def has(*w: str) -> bool:
        return any(x in notes for x in w)

    structuring = has("structuring", "just under", "cash deposit", "cash")
    fast = has("rapid", "layering", "pass-through", "passthrough", "in and out")
    other = has("offshore", "shell compan", "crypto", "anonymous wallet", "launder")

    txns: list[dict[str, Any]] = []

    def add(amount: float, direction: str, cp: str, channel: str = "wire",
            country: str = "") -> None:
        txns.append({"amount": round(amount, 2), "direction": direction,
                     "counterparty": cp, "channel": channel, "country": country})

    for _ in range(rng.randint(3, 5)):                       # salary in
        add(rng.uniform(1800, 4200), "in", "acme payroll", "transfer")
    for _ in range(rng.randint(3, 6)):                       # everyday spend out
        add(rng.uniform(50, 1200), "out",
            rng.choice(["landlord ltd", "local grocer", "utility co", "tax office"]),
            "card")
    if structuring:
        for _ in range(rng.randint(3, 5)):                   # cash just under CTR
            add(rng.uniform(0.90, 0.99) * thr, "in", "cash deposit", "cash")
    if fast:                                                 # pass-through pairs
        for cp, base in [("offshore holdings ltd", rng.uniform(9000, 15000)),
                         ("shell corp bvi", rng.uniform(6000, 12000))]:
            add(base, "in", cp, "wire", "Cayman Islands")
            add(base * rng.uniform(0.9, 0.98), "out", cp, "wire", "Cayman Islands")
    if other:
        add(rng.uniform(4000, 20000), "out", "anonymous wallet", "crypto")
        add(rng.uniform(4000, 20000), "in", "mixer service", "crypto")
    return txns


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
            "amount": round(amt, 2),
            "direction": "out" if str(t.get("direction", "in")).lower() == "out" else "in",
            "counterparty": str(t.get("counterparty") or "").strip(),
            "channel": str(t.get("channel") or "wire").lower(),
            "country": str(t.get("country") or "").strip(),
        })
    return out


def analyze_transactions(profile: dict[str, Any]) -> dict[str, Any]:
    """Analyse the ledger for AML typologies. Every flag maps to a real row."""
    thr = STRUCTURING_THRESHOLD
    provided = profile.get("transactions") or []
    ledger = _coerce_ledger(provided) if provided else _synthesize_ledger(profile)
    source = "analyst-provided" if provided else "synthesized (no ledger supplied)"
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
    crypto_total = round(sum(t["amount"] for t in enriched if t["channel"] == "crypto"), 2)

    near = [t for t in enriched if "near_threshold" in t["flags"]]
    structuring_detected = len(near) >= 3
    rapid_movement = len(passthrough_cps) > 0
    high_risk_hits = sorted({t["counterparty"] for t in enriched
                             if "high_risk_counterparty" in t["flags"] and t["counterparty"]})
    high_risk_countries = sorted({t["country"] for t in enriched
                                  if "high_risk_country" in t["flags"] and t["country"]})
    cash_ratio = round(cash_total / total, 3) if total else 0.0
    over_expected = bool(expected_monthly and total_in > 3 * expected_monthly)
    flagged = [t for t in enriched if t["flags"]]

    patterns: list[str] = []
    if structuring_detected:
        patterns.append(f"{len(near)} deposits just under the {thr:,.0f} reporting "
                        f"threshold (structuring / smurfing)")
    if rapid_movement:
        patterns.append(f"pass-through activity: funds routed in and back out via "
                        f"{', '.join(sorted(passthrough_cps))}")
    if cash_ratio >= 0.3:
        patterns.append(f"cash-intensive: {cash_ratio:.0%} of volume is cash")
    if crypto_total > 0:
        patterns.append(f"cryptocurrency exposure (${crypto_total:,.0f})")
    if high_risk_hits:
        patterns.append(f"exposure to high-risk counterparties: {', '.join(high_risk_hits)}")
    if over_expected:
        patterns.append(f"inflow ${total_in:,.0f} far exceeds expected "
                        f"${expected_monthly:,.0f}/mo")

    signals = sum([structuring_detected, rapid_movement, cash_ratio >= 0.3,
                   crypto_total > 0, bool(high_risk_hits), over_expected])
    return {
        "source": source,
        "transaction_count": len(enriched),
        "total_volume": total,
        "total_in": total_in,
        "total_out": total_out,
        "cash_total": cash_total,
        "cash_ratio": cash_ratio,
        "crypto_total": crypto_total,
        "largest_transaction": max((t["amount"] for t in enriched), default=0.0),
        "structuring_detected": structuring_detected,
        "near_threshold_count": len(near),
        "rapid_movement": rapid_movement,
        "passthrough_counterparties": sorted(passthrough_cps),
        "high_risk_counterparties": high_risk_hits,
        "high_risk_countries": high_risk_countries,
        "over_expected_volume": over_expected,
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
    """
    ledger = _coerce_ledger(profile.get("transactions") or []) \
        if profile.get("transactions") else _synthesize_ledger(profile)
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

    # 1) New account + high velocity → synthetic/first-party fraud
    if 0 < account_age < 30 and count >= 10:
        typologies.append({"type": "new_account_velocity", "weight": 30,
                           "detail": f"{count} transactions on a {account_age}-day-old account"})

    # 2) Card testing — several small card charges (stolen-card validation)
    card_tests = [t for t in ledger if t["channel"] == "card" and t["amount"] < 100]
    if len(card_tests) >= 3:
        typologies.append({"type": "card_testing", "weight": 25,
                           "detail": f"{len(card_tests)} small card charges (< $100)"})
        flag(card_tests, "card_testing")

    # 3) New-payee large transfer — classic APP/scam pattern
    new_payee_large = [t for t in out_txns
                       if t["amount"] >= 5_000 and cp_counts.get(t["counterparty"].lower(), 0) == 1]
    if new_payee_large:
        typologies.append({"type": "new_payee_large_transfer", "weight": 25,
                           "detail": f"{len(new_payee_large)} large transfer(s) "
                                     f"to first-time payees"})
        flag(new_payee_large, "new_payee_large_transfer")

    # 4) Rapid dispersal to many payees (mule cash-out)
    if len(distinct_out_payees) >= 4 and total_in > 0 and total_out >= 0.8 * total_in:
        typologies.append({"type": "rapid_dispersal", "weight": 25,
                           "detail": f"funds dispersed to {len(distinct_out_payees)} "
                                     f"distinct payees"})

    # 5) Geographic dispersion (possible account takeover / impossible travel proxy)
    if len(countries) >= 3:
        typologies.append({"type": "geographic_dispersion", "weight": 15,
                           "detail": f"counterparties across {len(countries)} countries"})

    # 6) Round-amount scripting (automated fraud often uses round numbers)
    round_amts = [t for t in ledger if t["amount"] >= 1_000 and t["amount"] % 1_000 == 0]
    if len(round_amts) >= 3:
        typologies.append({"type": "round_amount_scripting", "weight": 10,
                           "detail": f"{len(round_amts)} exact round-thousand amounts"})

    # 7) High value on a brand-new account
    if 0 < account_age < 14 and largest >= 10_000:
        typologies.append({"type": "high_value_new_account", "weight": 20,
                           "detail": f"${largest:,.0f} on a {account_age}-day-old "
                                     f"account"})

    fraud_score = min(100, sum(t["weight"] for t in typologies))
    band = _band(fraud_score)
    return {
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
def screen_sanctions(full_name: str, country: str = "") -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for entry in SANCTIONS_ENTRIES:
        score = name_match_score(full_name, entry["name"])
        if score >= 70:
            matches.append({
                "matched_name": entry["name"],
                "list_name": entry["list_name"],
                "program": entry["program"],
                "entity_type": entry.get("type", "individual"),
                "match_score": score,
            })
    matches.sort(key=lambda m: m["match_score"], reverse=True)
    highest = matches[0]["match_score"] if matches else 0

    if highest >= 85:
        tier, action = "STRONG", ("Block and escalate; confirm identity against the "
                                  "listed entity")
    elif highest >= 70:
        tier, action = "POSSIBLE", ("Manual review required — similarity below the "
                                    "auto-clear threshold")
    else:
        tier, action = "NONE", "No sanctions match"
    return {
        "screened_name": full_name,
        "matches": matches,
        "hit": highest >= 85,
        "match_tier": tier,
        "recommended_action": action,
        "highest_match_score": highest,
        "country_risk_tier": country_risk_tier(country),
        "blocked_country_exposure": country_risk_tier(country) == "BLOCKED",
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
