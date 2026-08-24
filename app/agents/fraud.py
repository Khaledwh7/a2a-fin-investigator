"""Fraud Agent — detects theft/deception typologies (distinct from AML).

Where the AML agent looks for money-laundering (hiding illicit funds via
structuring/layering), the Fraud agent looks for *fraud*: money being stolen or
moved by deception — account takeover, card testing, scam/new-payee transfers,
mule dispersal and synthetic-identity abuse. It analyses the same ledger, but
for a different intent, and contributes its own dimension to the risk score.
"""

from __future__ import annotations

from typing import Any

from app.agents.base import SpecialistExecutor
from app.agents.schemas import CustomerProfile


class FraudExecutor(SpecialistExecutor):
    role = "fraud"
    artifact_name = "fraud_findings"
    working_note = "screening for fraud typologies (ATO, card testing, scams, mules)"

    def analyze(self, profile: CustomerProfile, context: dict[str, Any]) -> dict[str, Any]:
        return self.registry.call("detect_fraud", self.role, profile=profile.model_dump())

    def summarize(self, findings: dict[str, Any]) -> str:
        if not findings.get("assessed", True):
            return "Not assessed — no transaction ledger supplied"
        n = findings["signal_count"]
        if n == 0:
            return "No fraud typologies detected"
        alert = " · FRAUD ALERT" if findings["fraud_alert"] else ""
        return (f"{n} fraud typolog{'y' if n == 1 else 'ies'} — "
                f"score {findings['fraud_score']}/100 ({findings['fraud_band']}){alert}")
