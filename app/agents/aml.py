"""AML Agent — Anti-Money-Laundering transaction analysis."""

from __future__ import annotations

from typing import Any

from app.agents.base import SpecialistExecutor
from app.agents.schemas import CustomerProfile


class AMLExecutor(SpecialistExecutor):
    role = "aml"
    artifact_name = "aml_findings"
    working_note = "analyzing transactions for structuring and layering"

    def analyze(self, profile: CustomerProfile, context: dict[str, Any]) -> dict[str, Any]:
        # The tool computes signal counts + the SAR candidacy from the real ledger.
        return self.registry.call("analyze_transactions", self.role,
                                  profile=profile.model_dump())

    def summarize(self, findings: dict[str, Any]) -> str:
        n = len(findings["suspicious_patterns"])
        vol = findings["total_volume"]
        if n == 0:
            return f"No AML red flags across {findings['transaction_count']} txns (${vol:,.0f})"
        return f"{n} AML red flag(s) across {findings['transaction_count']} txns (${vol:,.0f})"
