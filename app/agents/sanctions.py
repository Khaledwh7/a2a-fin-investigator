"""Sanctions Screening Agent — watchlist name matching + blocked-country check."""

from __future__ import annotations

from typing import Any

from app.agents.base import SpecialistExecutor
from app.agents.schemas import CustomerProfile


class SanctionsExecutor(SpecialistExecutor):
    role = "sanctions"
    artifact_name = "sanctions_findings"
    working_note = "screening against sanctions lists"

    def analyze(self, profile: CustomerProfile, context: dict[str, Any]) -> dict[str, Any]:
        # Screen the customer AND the beneficiaries in their ledger.
        counterparties = [{"name": t.counterparty, "country": t.country}
                          for t in profile.transactions if t.counterparty]
        return self.registry.call("screen_sanctions", self.role,
                                  full_name=profile.full_name, country=profile.country,
                                  counterparties=counterparties)

    def summarize(self, findings: dict[str, Any]) -> str:
        if findings["hit"]:
            top = findings["matches"][0]
            return (f"SANCTIONS HIT: {top['matched_name']} "
                    f"({top['list_name']}/{top['program']}, {top['match_score']}%)")
        if findings.get("counterparty_hit"):
            cp = findings["counterparty_matches"][0]
            return (f"BENEFICIARY SANCTIONS HIT: {cp['counterparty']} "
                    f"→ {cp['matched_name']} ({cp['program']})")
        if findings["matches"]:
            return f"Possible match at {findings['highest_match_score']}% — manual review"
        blocked = " · blocked-country exposure" if findings["blocked_country_exposure"] else ""
        return f"No sanctions match{blocked}"
