"""Agent Card construction — OFFICIAL A2A concept.

The Agent Card is a public JSON document that says who an agent is, where to
reach it, what it can do (skills), and how to authenticate. A2A servers publish
it at the well-known URI:

    https://{host}/.well-known/agent-card.json          (v1.0 path)

In our single-host demo each agent lives under its own path, so the card sits
at ``{agent-base}/.well-known/agent-card.json``. When we split into separate
containers, each agent is at a host root and the path becomes the canonical
one — no code change, just different URLs in the peer registry.

This module only *builds* generic cards. The six agents supply their own
name/description/skills in Phase 3.
"""

from __future__ import annotations

from app.a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
)

PROVIDER = AgentProvider(
    organization="AI Financial Investigation Assistant",
    url="https://github.com/your-handle/a2a-fin-investigator",
)


def build_agent_card(
    *,
    name: str,
    description: str,
    rpc_url: str,
    skills: list[AgentSkill],
    streaming: bool = True,
    version: str = "1.0.0",
) -> AgentCard:
    """Assemble a spec-shaped Agent Card for one agent.

    ``rpc_url`` is the JSON-RPC endpoint peers POST to; it goes into
    ``supportedInterfaces[0].url``.
    """
    return AgentCard(
        name=name,
        description=description,
        version=version,
        provider=PROVIDER,
        supported_interfaces=[
            AgentInterface(url=rpc_url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=streaming, push_notifications=False),
        skills=skills,
    )
