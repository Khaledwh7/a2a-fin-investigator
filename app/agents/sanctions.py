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
        result = self.registry.call("screen_sanctions", self.role,
                                    full_name=profile.full_name, country=profile.country)
        return result

    def summarize(self, findings: dict[str, Any]) -> str:
        if findings["hit"]:
            top = findings["matches"][0]
            return (f"SANCTIONS HIT: {top['matched_name']} "
                    f"({top['list_name']}/{top['program']}, {top['match_score']}%)")
        if findings["matches"]:
            return f"Possible match at {findings['highest_match_score']}% — manual review"
        blocked = " · blocked-country exposure" if findings["blocked_country_exposure"] else ""
        return f"No sanctions match{blocked}"
