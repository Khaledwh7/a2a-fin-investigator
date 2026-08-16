"""Risk Scoring Agent — a transparent, multi-dimensional risk-rating model.

Rather than one flat additive number, the score is built the way a real AML
customer-risk-rating model works: four independent **risk dimensions**, each
0–100 with its own contributing factors, then a weighted blend with **escalation
rules** (a strong sanctions hit alone forces CRITICAL; a blocked-country or a
PEP can't score LOW). Every dimension and every factor is returned, so the UI
can show a radar + a waterfall and the analyst can defend the rating.

The weights and rules are our modelling choices, not part of A2A.
"""

from __future__ import annotations

from typing import Any

from app.agents.base import SpecialistExecutor
from app.agents.schemas import CustomerProfile, band_for
from app.tools.datasets import country_risk_score

# Blend weights across the five risk dimensions (sum to 1.0).
_WEIGHTS = {"sanctions": 0.25, "transaction": 0.22, "fraud": 0.20,
            "geographic": 0.18, "customer": 0.15}


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

        blend = sum(_WEIGHTS[k] * d["score"] for k, d in dims.items())
        overall = round(blend)

        # --- escalation rules (a single strong signal must not be diluted) ---
        escalations: list[str] = []
        if sanctions.get("match_tier") == "STRONG":
            overall = max(overall, 90)
            escalations.append("confirmed sanctions match → CRITICAL floor")
        if sanctions.get("blocked_country_exposure"):
            overall = max(overall, 60)
            escalations.append("exposure to a blocked jurisdiction → HIGH floor")
        if aml.get("structuring_detected") and aml.get("rapid_movement"):
            overall = max(overall, 60)
            escalations.append("structuring + layering together → HIGH floor")
        if fraud.get("fraud_score", 0) >= 70:
            overall = max(overall, 60)
            escalations.append("high fraud score → HIGH floor")
        if kyc.get("is_pep") or kyc.get("declared_pep"):
            overall = max(overall, 30)
            escalations.append("PEP → MEDIUM floor (EDD required)")
        overall = min(100, overall)
        band = band_for(overall)

        # Flatten factors + a waterfall over the weighted dimension contributions.
        factors = [f for d in dims.values() for f in d["factors"]]
        breakdown, running = [], 0.0
        for key in ("sanctions", "transaction", "fraud", "geographic", "customer"):
            contribution = round(_WEIGHTS[key] * dims[key]["score"], 1)
            running += contribution
            breakdown.append({"factor": key, "weight": contribution,
                              "running_total": round(min(100, running), 1)})

        sar = (band in {"HIGH", "CRITICAL"} or bool(aml.get("sar_candidate"))
               or bool(fraud.get("fraud_alert")))
        return {
            "risk_score": overall,
            "risk_band": band,
            "dimensions": {k: {"score": d["score"], "factors": d["factors"]}
                           for k, d in dims.items()},
            "contributing_factors": factors,
            "score_breakdown": breakdown,
            "escalations": escalations,
            "confidence": self._confidence(kyc, aml, sanctions),
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
        if s.get("blocked_country_exposure"):
            score = max(score, 60)
            factors.append({"factor": "blocked_country", "weight": 60,
                            "detail": "counterparty/residence in a blocked jurisdiction"})
        return {"score": min(100, score), "factors": factors}

    @staticmethod
    def _transaction_dim(a: dict) -> dict[str, Any]:
        factors, score = [], 0

        def add(points: int, key: str, detail: str) -> None:
            nonlocal score
            score += points
            factors.append({"factor": key, "weight": points, "detail": detail})

        if a.get("structuring_detected"):
            add(35, "structuring", f"{a.get('near_threshold_count', 0)} near-threshold deposits")
        if a.get("rapid_movement"):
            add(25, "rapid_movement",
                "pass-through via " + ", ".join(a.get("passthrough_counterparties", [])))
        if a.get("cash_ratio", 0) >= 0.3:
            add(20, "cash_intensive", f"{a.get('cash_ratio', 0):.0%} of volume is cash")
        if a.get("crypto_total", 0) > 0:
            add(15, "crypto_exposure", f"${a.get('crypto_total', 0):,.0f} via crypto")
        if a.get("high_risk_counterparties"):
            add(20, "high_risk_counterparties", ", ".join(a["high_risk_counterparties"]))
        if a.get("over_expected_volume"):
            add(15, "volume_over_expected", "inflow far exceeds expected volume")
        return {"score": min(100, score), "factors": factors}

    @staticmethod
    def _fraud_dim(f: dict) -> dict[str, Any]:
        factors = [{"factor": t["type"], "weight": t["weight"], "detail": t["detail"]}
                   for t in f.get("typologies", [])]
        return {"score": min(100, f.get("fraud_score", 0)), "factors": factors}

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
        if cp_max >= 70:
            factors.append({"factor": "counterparty_geography", "weight": cp_max,
                            "detail": "high-risk counterparty countries: "
                                      + ", ".join(cp_countries)})
        return {"score": min(100, max(residence, nat, tax, cp_max)), "factors": factors}

    @staticmethod
    def _customer_dim(kyc: dict) -> dict[str, Any]:
        factors, score = [], 0

        def add(points: int, key: str, detail: str) -> None:
            nonlocal score
            score += points
            factors.append({"factor": key, "weight": points, "detail": detail})

        if kyc.get("is_pep"):
            add(60, "pep", kyc.get("pep_role") or "politically exposed person")
        elif kyc.get("declared_pep"):
            add(45, "declared_pep", "self-declared politically exposed person")
        ind = kyc.get("industry_risk", 0)
        if ind >= 40:
            pts = 30 if ind >= 70 else 18 if ind >= 50 else 10
            add(pts, "industry_risk", f"high-risk industry ({kyc.get('industry')})")
        if kyc.get("remote_onboarding"):
            add(12, "non_face_to_face", "remote (non-face-to-face) onboarding")
        if kyc.get("income_mismatch"):
            add(20, "income_mismatch", "expected activity exceeds declared income")
        dq = kyc.get("data_quality_score", 100)
        if dq < 75:
            add(round((100 - dq) * 0.5), "identity_incomplete",
                "missing/invalid: " + ", ".join(kyc.get("missing_fields", [])))
        return {"score": min(100, score), "factors": factors}

    # --- confidence ------------------------------------------------------
    @staticmethod
    def _confidence(kyc: dict, aml: dict, sanctions: dict) -> dict[str, Any]:
        data_quality = kyc.get("data_quality_score", 50)
        evidence = min(100, 30 + aml.get("transaction_count", 0) * 6)
        value = round(0.5 * data_quality + 0.5 * evidence)
        if sanctions.get("match_tier") == "POSSIBLE":
            value = max(0, value - 10)  # unresolved match ⇒ less certain
        label = "HIGH" if value >= 75 else "MEDIUM" if value >= 50 else "LOW"
        return {"value": value, "label": label,
                "basis": f"data quality {data_quality}/100, "
                         f"{aml.get('transaction_count', 0)} transactions analysed"}

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
