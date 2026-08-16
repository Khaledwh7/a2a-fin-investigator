"""KYC Agent — Know Your Customer: identity and PEP checks."""

from __future__ import annotations

from typing import Any

from app.agents.base import SpecialistExecutor
from app.agents.schemas import CustomerProfile


class KYCExecutor(SpecialistExecutor):
    role = "kyc"
    artifact_name = "kyc_findings"
    working_note = "verifying identity and screening for PEP status"

    def analyze(self, profile: CustomerProfile, context: dict[str, Any]) -> dict[str, Any]:
        identity = self.registry.call("verify_identity", self.role,
                                      profile=profile.model_dump())
        pep = self.registry.call("screen_pep", self.role, full_name=profile.full_name)

        flags: list[str] = list(identity["issues"])
        if pep["is_pep"]:
            flags.append(f"politically exposed person ({pep['pep_role']})")
        elif identity["declared_pep"]:
            flags.append("self-declared politically exposed person")
        if identity["residence_high_risk"]:
            flags.append(f"resident in a high-risk jurisdiction ({profile.country})")
        if identity["nationality_risk"] >= 70 and profile.nationality:
            flags.append(f"national of a high-risk jurisdiction ({profile.nationality})")
        if identity["industry_risk"] >= 50 and identity["industry"]:
            flags.append(f"high-risk industry ({identity['industry']})")
        if identity["remote_onboarding"]:
            flags.append("non-face-to-face (remote) onboarding")
        if identity["income_mismatch"]:
            flags.append("expected activity inconsistent with declared income")

        return {
            "identity_verified": identity["identity_verified"],
            "document_valid": identity["document_valid"],
            "data_quality_score": identity["data_quality_score"],
            "checks": identity["checks"],                 # field-level detail
            "missing_fields": identity["missing_fields"],
            "is_pep": pep["is_pep"],
            "pep_role": pep["pep_role"],
            "pep_match_score": pep["pep_match_score"],
            "declared_pep": identity["declared_pep"],
            "residence_country": identity["residence_country"],
            "residence_risk_tier": identity["residence_risk_tier"],
            "residence_high_risk": identity["residence_high_risk"],
            "nationality": identity["nationality"],
            "nationality_risk": identity["nationality_risk"],
            "tax_residency": identity["tax_residency"],
            "tax_residency_risk": identity["tax_residency_risk"],
            "industry": identity["industry"],
            "industry_risk": identity["industry_risk"],
            "remote_onboarding": identity["remote_onboarding"],
            "income_mismatch": identity["income_mismatch"],
            "risk_flags": flags,
        }

    def summarize(self, findings: dict[str, Any]) -> str:
        state = "verified" if findings["identity_verified"] else "NOT verified"
        pep = " · PEP" if findings["is_pep"] else ""
        return f"Identity {state} (quality {findings['data_quality_score']}/100){pep}"
