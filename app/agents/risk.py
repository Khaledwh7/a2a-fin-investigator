"""Risk Scoring Agent — a transparent, multi-dimensional risk-rating model.

Rather than one flat additive number, the score is built the way a real AML
customer-risk-rating model works: five independent **risk dimensions**, each
0–100 with its own contributing factors, then a weighted blend with **escalation
rules** (a strong sanctions hit alone forces CRITICAL; a blocked-country or a
PEP can't score LOW). Every dimension and every factor is returned, so the UI
can show a radar + a waterfall and the analyst can defend the rating.

A dimension with no evidence behind it is marked **not assessed** and dropped
from the blend (the remaining weights renormalise) rather than scored 0. Scoring
absent evidence as zero is what makes an unscreened customer look safe; saying
"not assessed" — and cutting the confidence accordingly — is what a rating a
regulator can read looks like.

The weights and rules are our modelling choices, not part of A2A.
"""

from __future__ import annotations

from typing import Any

from app.agents.base import SpecialistExecutor
from app.agents.schemas import RISK_BANDS, CustomerProfile, band_for
from app.tools.datasets import country_risk_score

# Blend weights across the five risk dimensions (sum to 1.0). Public because the
# report documents them — a score you can't see the weights for isn't defensible.
DIMENSION_WEIGHTS = {"sanctions": 0.25, "transaction": 0.22, "fraud": 0.20,
                     "geographic": 0.18, "customer": 0.15}
_WEIGHTS = DIMENSION_WEIGHTS
_DIM_ORDER = ("sanctions", "transaction", "fraud", "geographic", "customer")
# Below this many rows the pattern detectors (structuring needs 3 near-threshold
# deposits, velocity needs 6-8 in a window) cannot fire, so a ledger this thin
# cannot support a confident rating either.
_MIN_PATTERN_ROWS = 5


def _ramp(value: float, lo: float, hi: float) -> float:
    """Position of ``value`` between ``lo`` and ``hi``, clamped to 0–1.

    The unit of magnitude-sensitive scoring: a detector contributes a base score
    for firing at all, plus a share of its headroom for how hard it fired.
    """
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def _combine(points: list[float]) -> int:
    """Combine independent indicators without a hard ceiling.

    A plain sum saturates: past a handful of findings everything reads 100 and
    the worst cases become indistinguishable. Noisy-OR treats each indicator as
    independent evidence, so each new finding still moves the score while the
    increments shrink. Every indicator at its own maximum comes to ~90 rather
    than pinning at 100, which leaves the top of the range meaningful.
    """
    remaining = 1.0
    for pts in points:
        remaining *= 1.0 - max(0.0, min(99.0, pts)) / 100.0
    return round(100 * (1.0 - remaining))


def _band_ceiling(floor: int) -> int:
    """Highest score still inside the band that ``floor`` sits in."""
    above = [t for t, _ in RISK_BANDS if t > floor]
    return min(above) - 1 if above else 100


def _apply_floor(blend: float, floor: int) -> int:
    """Raise a score to an escalation floor **without discarding the evidence**.

    A floor exists to guarantee a band ("a confirmed sanctions match is CRITICAL"),
    but a plain ``max(score, floor)`` also flattens everything above it: a
    sanctioned customer who is otherwise clean and one who is also structuring
    through a blocked jurisdiction both land on exactly 90, and an analyst
    working a queue can no longer tell them apart. Mapping the blend into the
    headroom between the floor and the top of its band keeps the guarantee and
    restores the ordering within it.
    """
    ceiling = _band_ceiling(floor)
    return max(round(blend), round(floor + (ceiling - floor) * blend / 100))


def _dim(score: int, factors: list[dict[str, Any]], *, assessed: bool = True,
         evidence: str = "profile", reason: str = "") -> dict[str, Any]:
    """One risk dimension, with the provenance of the evidence behind it."""
    return {"score": min(100, max(0, score)), "factors": factors,
            "assessed": assessed, "evidence": evidence, "reason": reason}


class RiskExecutor(SpecialistExecutor):
    role = "risk"
    artifact_name = "risk_assessment"
    working_note = "computing multi-dimensional risk rating"

    def analyze(self, profile: CustomerProfile, context: dict[str, Any]) -> dict[str, Any]:
        kyc = context.get("kyc") or {}
        aml = context.get("aml") or {}
        sanctions = context.get("sanctions") or {}
        fraud = context.get("fraud") or {}

        dims = {
            "sanctions": self._sanctions_dim(sanctions),
            "transaction": self._transaction_dim(aml),
            "fraud": self._fraud_dim(fraud),
            "geographic": self._geographic_dim(profile, kyc, aml),
            "customer": self._customer_dim(kyc),
        }

        # --- weighted blend over the dimensions we could actually assess ------
        assessed = {k: d for k, d in dims.items() if d["assessed"]}
        weight_sum = sum(_WEIGHTS[k] for k in assessed) or 1.0
        blend = sum(_WEIGHTS[k] * d["score"] for k, d in assessed.items()) / weight_sum
        overall = round(blend)
        unassessed = [k for k in _DIM_ORDER if not dims[k]["assessed"]]

        # --- escalation rules (a single strong signal must not be diluted) ---
        # Floors are collected first, then the highest is applied once: applying
        # each in turn with max() would pin the score to the floor value and throw
        # away every other signal (see _apply_floor).
        escalations: list[str] = []
        floors: list[int] = []
        if sanctions.get("match_tier") == "STRONG":
            floors.append(90)
            escalations.append("confirmed sanctions match → CRITICAL floor")
        if sanctions.get("counterparty_hit"):
            floors.append(90)
            escalations.append("payment to a sanctioned beneficiary → CRITICAL floor")
        if sanctions.get("blocked_country_exposure"):
            floors.append(60)
            escalations.append("exposure to a blocked jurisdiction → HIGH floor")
        attested = {o["indicator"] for o in aml.get("attested_observations", [])}
        if aml.get("structuring_detected") and aml.get("rapid_movement"):
            floors.append(60)
            escalations.append("structuring + layering together → HIGH floor")
        elif {"structuring", "rapid_movement"} <= attested:
            floors.append(60)
            escalations.append("analyst-attested structuring + layering → HIGH floor "
                               "(unverified — obtain the ledger to confirm)")
        if fraud.get("fraud_score", 0) >= 70:
            floors.append(60)
            escalations.append("high fraud score → HIGH floor")
        if kyc.get("is_pep") or kyc.get("declared_pep"):
            floors.append(30)
            escalations.append("PEP → MEDIUM floor (EDD required)")
        # Coherence rule: a rating of LOW next to "SAR recommended" is a report
        # that contradicts itself. Nine deposits at 99% of the reporting
        # threshold is a filing-worthy finding even when nothing else is wrong,
        # and a single dimension at 22% of the blend cannot say so on its own.
        if aml.get("sar_candidate") or fraud.get("fraud_alert"):
            floors.append(25)
            escalations.append("SAR-candidate evidence → MEDIUM floor "
                               "(a case that warrants a filing cannot be LOW)")
        if floors:
            applied = max(floors)
            overall = _apply_floor(blend, applied)
            escalations.append(
                f"floor {applied} applied to a blend of {round(blend)}; the remaining "
                f"evidence orders the case within the band (final {overall})")
        overall = min(100, overall)
        band = band_for(overall)

        # Flatten factors + a waterfall over the weighted dimension contributions.
        # Contributions use the renormalised weights, so the bars always add up to
        # the blended score the analyst sees.
        factors = [f for d in dims.values() for f in d["factors"]]
        breakdown, running = [], 0.0
        for key in _DIM_ORDER:
            if not dims[key]["assessed"]:
                breakdown.append({"factor": key, "weight": 0.0, "assessed": False,
                                  "running_total": round(min(100, running), 1)})
                continue
            contribution = round(_WEIGHTS[key] / weight_sum * dims[key]["score"], 1)
            running += contribution
            breakdown.append({"factor": key, "weight": contribution, "assessed": True,
                              "running_total": round(min(100, running), 1)})

        sar = (band in {"HIGH", "CRITICAL"} or bool(aml.get("sar_candidate"))
               or bool(fraud.get("fraud_alert")))
        return {
            "risk_score": overall,
            "risk_band": band,
            "dimensions": {k: {"score": d["score"], "factors": d["factors"],
                               "assessed": d["assessed"], "evidence": d["evidence"],
                               "reason": d["reason"]}
                           for k, d in dims.items()},
            "unassessed_dimensions": unassessed,
            "coverage_pct": round(100 * weight_sum) if assessed else 0,
            "contributing_factors": factors,
            "score_breakdown": breakdown,
            "escalations": escalations,
            "confidence": self._confidence(kyc, aml, sanctions, dims, weight_sum),
            "sar_recommended": sar,
            "decision": self._decision(band),
            "recommendation": self._recommendation(band),
            "recommended_actions": self._actions(band, kyc, aml, sanctions, fraud),
        }

    # --- dimensions ------------------------------------------------------
    @staticmethod
    def _sanctions_dim(s: dict) -> dict[str, Any]:
        factors, score = [], 0
        tier = s.get("match_tier", "NONE")
        if tier == "STRONG":
            top = (s.get("matches") or [{}])[0]
            score = 100
            factors.append({"factor": "sanctions_hit", "weight": 100,
                            "detail": f"matched {top.get('matched_name')} "
                                      f"({top.get('program')}, {top.get('match_score')}%)"})
        elif tier == "POSSIBLE":
            score = 55
            factors.append({"factor": "possible_sanctions_match", "weight": 55,
                            "detail": f"name similarity {s.get('highest_match_score')}%"})
        if s.get("counterparty_hit"):
            cp = (s.get("counterparty_matches") or [{}])[0]
            score = 100
            factors.append({"factor": "beneficiary_sanctions_hit", "weight": 100,
                            "detail": f"pays {cp.get('counterparty')} → "
                                      f"{cp.get('matched_name')} ({cp.get('program')})"})
        if s.get("blocked_country_exposure"):
            score = max(score, 60)
            factors.append({"factor": "blocked_country", "weight": 60,
                            "detail": "counterparty/residence in a blocked jurisdiction"})
        return _dim(score, factors, evidence="watchlist screening")

    @staticmethod
    def _transaction_dim(a: dict) -> dict[str, Any]:
        """Scored from the ledger when there is one; from the analyst's attested
        observations when there isn't; not assessed when there is neither.

        Each indicator contributes a base for firing plus a share of its headroom
        for *how hard* it fired, so ten deposits of 9,950 outscore three of 8,600
        instead of both reading "structuring: yes".
        """
        factors: list[dict[str, Any]] = []
        points: list[float] = []

        def add(pts: float, key: str, detail: str) -> None:
            points.append(pts)
            factors.append({"factor": key, "weight": round(pts), "detail": detail})

        if not a.get("ledger_available", True):
            observed = a.get("attested_observations", [])
            if not observed:
                return _dim(0, [], assessed=False, evidence="none",
                            reason=a.get("unassessed_reason",
                                         "no transaction ledger supplied"))
            for o in observed:
                add(o["weight"], o["indicator"],
                    f"{o['detail']} — analyst-attested, unverified")
            return _dim(_combine(points), factors, evidence="analyst-attested",
                        reason="No ledger supplied; scored from the analyst's observed "
                               "activity, which is unverified.")

        if a.get("structuring_detected"):
            count = a.get("near_threshold_count", 0)
            proximity = a.get("near_threshold_proximity", 0.0)
            # More deposits, and deposits sitting tighter under the limit, are
            # more deliberate — and score higher.
            add(22 + 13 * _ramp(count, 3, 9) + 8 * proximity, "structuring",
                f"{count} deposits just under the limit, averaging "
                f"{85 + 15 * proximity:.0f}% of it")
        if a.get("rapid_movement"):
            symmetry = a.get("passthrough_symmetry", 0.0)
            share = a.get("passthrough_volume_share", 0.0)
            # Money in and straight back out in equal measure, moving a large
            # share of the account, is the strongest layering signal.
            add(14 + 11 * symmetry + 8 * _ramp(share, 0.1, 0.6), "rapid_movement",
                f"pass-through via {', '.join(a.get('passthrough_counterparties', []))} "
                f"— {symmetry:.0%} in/out symmetry over {share:.0%} of volume")
        if a.get("high_velocity"):
            burst = a.get("velocity_max_7d", 0)
            add(8 + 14 * _ramp(burst, 8, 25), "velocity_spike",
                f"{burst} transactions in a 7-day window")
        if a.get("cash_ratio", 0) >= 0.3:
            ratio = a.get("cash_ratio", 0)
            add(10 + 25 * _ramp(ratio, 0.3, 1.0), "cash_intensive",
                f"{ratio:.0%} of {a.get('cash_ratio_basis', 'volume')} is cash")
        if a.get("crypto_total", 0) > 0:
            share = a.get("crypto_ratio", 0.0)
            add(6 + 16 * _ramp(share, 0.0, 0.5), "crypto_exposure",
                f"${a.get('crypto_total', 0):,.0f} via crypto ({share:.0%} of volume)")
        if a.get("high_risk_counterparties"):
            share = a.get("high_risk_volume_share", 0.0)
            add(9 + 18 * _ramp(share, 0.05, 0.6), "high_risk_counterparties",
                f"{', '.join(a['high_risk_counterparties'])} — {share:.0%} of volume")
        if a.get("over_expected_volume"):
            ratio = a.get("over_expected_ratio", 0.0)
            add(7 + 15 * _ramp(ratio, 3, 12), "volume_over_expected",
                f"inflow is {ratio:.1f}x the expected monthly volume")
        return _dim(_combine(points), factors,
                    evidence=f"{a.get('transaction_count', 0)} transactions")

    @staticmethod
    def _fraud_dim(f: dict) -> dict[str, Any]:
        if not f.get("assessed", True):
            return _dim(0, [], assessed=False, evidence="none",
                        reason=f.get("unassessed_reason",
                                     "no transaction ledger supplied"))
        factors = [{"factor": t["type"], "weight": t["weight"], "detail": t["detail"]}
                   for t in f.get("typologies", [])]
        return _dim(f.get("fraud_score", 0), factors, evidence="ledger typologies")

    @staticmethod
    def _geographic_dim(profile: CustomerProfile, kyc: dict, aml: dict) -> dict[str, Any]:
        factors = []
        residence = country_risk_score(profile.country)
        if residence >= 40:
            factors.append({"factor": "residence_country", "weight": residence,
                            "detail": f"{profile.country} "
                                      f"({kyc.get('residence_risk_tier', '')})"})
        nat = kyc.get("nationality_risk", 0)
        if nat >= 40 and kyc.get("nationality"):
            factors.append({"factor": "nationality", "weight": nat,
                            "detail": f"national of {kyc.get('nationality')}"})
        tax = kyc.get("tax_residency_risk", 0)
        if tax >= 40 and kyc.get("tax_residency"):
            factors.append({"factor": "tax_residency", "weight": tax,
                            "detail": f"tax resident of {kyc.get('tax_residency')}"})
        cp_countries = aml.get("high_risk_countries", [])
        cp_max = max((country_risk_score(c) for c in cp_countries), default=0)
        cp_share = aml.get("high_risk_country_share", 0.0)
        if cp_max >= 70:
            # A token payment to a high-risk country is not the same as routing
            # the account through it, so the exposure scales with the share.
            cp_max = round(cp_max * (0.45 + 0.55 * _ramp(cp_share, 0.05, 0.5)))
            factors.append({"factor": "counterparty_geography", "weight": cp_max,
                            "detail": f"high-risk counterparty countries: "
                                      f"{', '.join(cp_countries)} — {cp_share:.0%} "
                                      f"of volume"})

        # The worst single jurisdiction leads, but the others still count: a
        # customer exposed on four fronts is riskier than one exposed on one,
        # which a plain max() cannot express.
        components = [residence, nat, tax, cp_max]
        primary = max(components)
        rest = sorted(components, reverse=True)[1:]
        secondary = sum(rest) / len(rest) if rest else 0.0
        score = round(primary + (100 - primary) * 0.30 * (secondary / 100))
        return _dim(score, factors,
                    evidence="residence / nationality / tax residency / counterparties")

    @staticmethod
    def _customer_dim(kyc: dict) -> dict[str, Any]:
        factors: list[dict[str, Any]] = []
        points: list[float] = []

        def add(pts: float, key: str, detail: str) -> None:
            points.append(pts)
            factors.append({"factor": key, "weight": round(pts), "detail": detail})

        if kyc.get("is_pep"):
            # A stronger name match is a more certain PEP identification.
            match = kyc.get("pep_match_score", 100)
            add(50 + 15 * _ramp(match, 88, 100), "pep",
                f"{kyc.get('pep_role') or 'politically exposed person'} "
                f"({match}% name match)")
        elif kyc.get("declared_pep"):
            add(45, "declared_pep", "self-declared politically exposed person")
        ind = kyc.get("industry_risk", 0)
        if ind >= 40:
            # Scale straight off the industry's own risk score rather than
            # bucketing it into three fixed steps.
            add(8 + 24 * _ramp(ind, 40, 85), "industry_risk",
                f"{kyc.get('industry')} (industry risk {ind}/100)")
        if kyc.get("remote_onboarding"):
            add(12, "non_face_to_face", "remote (non-face-to-face) onboarding")
        if kyc.get("income_mismatch"):
            add(20, "income_mismatch", "expected activity exceeds declared income")
        dq = kyc.get("data_quality_score", 100)
        if dq < 75:
            add((100 - dq) * 0.5, "identity_incomplete",
                f"data quality {dq}/100 — missing/invalid: "
                + ", ".join(kyc.get("missing_fields", [])))
        return _dim(_combine(points), factors, evidence="KYC record")

    # --- confidence ------------------------------------------------------
    @staticmethod
    def _confidence(kyc: dict, aml: dict, sanctions: dict, dims: dict,
                    weight_sum: float) -> dict[str, Any]:
        """How much the rating can be relied on — driven by what was assessed.

        Three honest inputs: the completeness of the KYC record, how much of the
        model could be scored at all (coverage), and how much *verified*
        transaction evidence sat behind it. An attested-only rating is capped,
        because an observation is not a measurement.
        """
        data_quality = kyc.get("data_quality_score", 50)
        coverage = round(100 * weight_sum)
        txns = aml.get("transaction_count", 0)
        # A handful of rows is thin evidence; ~13 gives a meaningful pattern read.
        evidence = min(100, txns * 8) if txns else 0
        value = round(0.35 * data_quality + 0.30 * coverage + 0.35 * evidence)

        # The additive terms alone floor a complete KYC record at ~65, so without
        # a ceiling a single transaction still reads MEDIUM. Cap by how much
        # transaction evidence there actually is: below the minimum needed to
        # trigger the pattern detectors, no rating can be better than LOW.
        cap, cap_reason = 100, ""
        if not aml.get("ledger_available", True):
            cap, cap_reason = 40, "no transaction evidence"
        elif txns < _MIN_PATTERN_ROWS:
            cap = 49
            cap_reason = (f"only {txns} transaction(s) — fewer than the "
                          f"{_MIN_PATTERN_ROWS} needed to establish a pattern")
        value = min(value, cap)

        if sanctions.get("match_tier") == "POSSIBLE":
            value = max(0, value - 10)  # unresolved match ⇒ less certain
        value = max(0, min(100, value))

        missing = [k for k, d in dims.items() if not d["assessed"]]
        basis = (f"data quality {data_quality}/100 · {coverage}% of the model assessed · "
                 + (f"{txns} transactions analysed" if txns
                    else "no transaction evidence"))
        if missing:
            basis += f" · not assessed: {', '.join(missing)}"
        if cap_reason:
            basis += f" · capped at {cap} ({cap_reason})"
        label = "HIGH" if value >= 75 else "MEDIUM" if value >= 50 else "LOW"
        return {"value": value, "label": label, "basis": basis,
                "coverage_pct": coverage, "unassessed": missing}

    # --- decision + actions ---------------------------------------------
    @staticmethod
    def _decision(band: str) -> str:
        return {"CRITICAL": "DECLINE & FILE SAR", "HIGH": "ENHANCED DUE DILIGENCE",
                "MEDIUM": "STANDARD DUE DILIGENCE", "LOW": "APPROVE"}[band]

    @staticmethod
    def _recommendation(band: str) -> str:
        return {
            "CRITICAL": "Freeze onboarding. File a SAR and escalate to compliance immediately.",
            "HIGH": "Apply Enhanced Due Diligence (EDD); consider filing a SAR.",
            "MEDIUM": "Apply standard due diligence with periodic review.",
            "LOW": "Proceed with standard monitoring.",
        }[band]

    @staticmethod
    def _actions(band: str, kyc: dict, aml: dict, sanctions: dict,
                 fraud: dict) -> list[str]:
        base = {
            "CRITICAL": ["Freeze onboarding / account activity",
                         "File a Suspicious Activity Report (SAR)",
                         "Escalate to the MLRO / compliance immediately",
                         "Do not tip off the customer"],
            "HIGH": ["Apply Enhanced Due Diligence (EDD)",
                     "Request documentary source-of-funds evidence",
                     "Obtain senior compliance sign-off",
                     "Increase ongoing monitoring frequency"],
            "MEDIUM": ["Apply standard due diligence",
                       "Schedule periodic review (12 months)",
                       "Monitor for changes in transaction patterns"],
            "LOW": ["Proceed with standard monitoring", "Routine periodic review"],
        }
        actions = list(base[band])
        # The most useful next step for an under-evidenced case is getting the
        # evidence, so it leads the list.
        if not aml.get("ledger_available", True):
            actions.insert(0, "Obtain the account's transaction ledger — AML and fraud "
                              "analysis could not be performed without it")
            if aml.get("attested_observations"):
                actions.insert(1, "Corroborate the analyst's observed activity against "
                                  "the ledger before relying on this rating")
        if sanctions.get("hit"):
            actions.insert(0, "Confirm the sanctions match against the official list "
                              "and block transactions")
        elif sanctions.get("matches"):
            actions.append("Manual sanctions review — possible name match")
        if kyc.get("is_pep"):
            actions.append("Obtain a PEP declaration and senior-management approval")
        if aml.get("structuring_detected"):
            actions.append("Review cash-deposit activity for structuring")
        if aml.get("rapid_movement"):
            actions.append("Investigate pass-through counterparties for layering")
        if fraud.get("fraud_alert"):
            types = ", ".join(t["type"].replace("_", " ") for t in fraud.get("typologies", []))
            actions.append(f"Open a fraud case — typologies: {types}")
        if not kyc.get("identity_verified", True):
            actions.append("Re-verify identity documents before proceeding")
        return actions

    def summarize(self, findings: dict[str, Any]) -> str:
        return (f"Risk {findings['risk_score']}/100 → {findings['risk_band']}"
                + (" · SAR recommended" if findings["sar_recommended"] else ""))
