"""The six Agent Cards — domain identity for each role.

Cards live in the domain layer (app/agents), not the protocol layer, because
the *content* (skills, descriptions) is domain-specific. The *shape* comes from
`app/a2a/agent_card.py`.
"""

from __future__ import annotations

from app.a2a.agent_card import build_agent_card
from app.a2a.types import AgentCard, AgentSkill
from app.config import AgentRole

_SKILLS: dict[AgentRole, list[AgentSkill]] = {
    AgentRole.ORCHESTRATOR: [
        AgentSkill(id="run_investigation", name="Run investigation",
                   description="Plan and coordinate a full financial-crime investigation "
                               "across KYC, AML, sanctions, risk and reporting agents.",
                   tags=["orchestration", "workflow"],
                   examples=["Investigate customer Viktor Petrov"]),
    ],
    AgentRole.KYC: [
        AgentSkill(id="verify_identity", name="Verify identity",
                   description="Validate identity documents and screen for PEP status.",
                   tags=["kyc", "identity", "pep"]),
    ],
    AgentRole.AML: [
        AgentSkill(id="analyze_transactions", name="Analyze transactions",
                   description="Detect structuring, layering and high-risk counterparties.",
                   tags=["aml", "transactions"]),
    ],
    AgentRole.SANCTIONS: [
        AgentSkill(id="screen_sanctions", name="Screen sanctions",
                   description="Fuzzy-match against sanctions lists and blocked countries.",
                   tags=["sanctions", "screening", "ofac"]),
    ],
    AgentRole.FRAUD: [
        AgentSkill(id="detect_fraud", name="Detect fraud",
                   description="Detect fraud typologies: account takeover, card testing, "
                               "scam/new-payee transfers and mule dispersal.",
                   tags=["fraud", "ato", "scams", "cards"]),
    ],
    AgentRole.RISK: [
        AgentSkill(id="score_risk", name="Score risk",
                   description="Aggregate KYC/AML/sanctions findings into a risk score.",
                   tags=["risk", "scoring"]),
    ],
    AgentRole.REPORTING: [
        AgentSkill(id="write_report", name="Write report",
                   description="Compose the final investigation report and recommendation.",
                   tags=["reporting", "narrative"]),
    ],
}

_DESCRIPTIONS: dict[AgentRole, str] = {
    AgentRole.ORCHESTRATOR: "Coordinates the investigation and delegates to specialists over A2A.",
    AgentRole.KYC: "Know-Your-Customer identity verification and PEP screening.",
    AgentRole.AML: "Anti-Money-Laundering transaction pattern analysis.",
    AgentRole.SANCTIONS: "Sanctions and watchlist screening.",
    AgentRole.FRAUD: "Fraud detection: account takeover, card testing, scams and mules.",
    AgentRole.RISK: "Risk aggregation and scoring.",
    AgentRole.REPORTING: "Final investigation report generation.",
}

_NAMES: dict[AgentRole, str] = {
    AgentRole.ORCHESTRATOR: "Orchestrator Agent",
    AgentRole.KYC: "KYC Agent",
    AgentRole.AML: "AML Agent",
    AgentRole.SANCTIONS: "Sanctions Screening Agent",
    AgentRole.FRAUD: "Fraud Detection Agent",
    AgentRole.RISK: "Risk Scoring Agent",
    AgentRole.REPORTING: "Reporting Agent",
}


def build_card(role: AgentRole, rpc_url: str) -> AgentCard:
    return build_agent_card(
        name=_NAMES[role],
        description=_DESCRIPTIONS[role],
        rpc_url=rpc_url,
        skills=_SKILLS[role],
    )


def build_all_cards(peer_urls: dict[AgentRole, str]) -> dict[AgentRole, AgentCard]:
    return {role: build_card(role, url) for role, url in peer_urls.items()}
