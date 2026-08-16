"""RBAC — Authorization: *what are you allowed to do?*

Authentication proves identity; authorization decides what that identity may do.
We express permissions as **scopes** and grant each identity the *minimum* set it
needs (least privilege). Two things fall out of this design:

  * A compromised **specialist** token can call *nobody* — specialists have no
    outbound scopes.
  * A compromised **user** token can start an investigation but cannot call KYC,
    AML, Sanctions, etc. directly — it lacks the ``a2a:invoke:*`` scopes.
  * Only the **orchestrator** token carries ``a2a:invoke:*`` — and only for the
    five specialists, nothing else.

This is the *transport-level* authorization (who may call whom). Tool-level
least privilege (which agent may call which tool) is enforced separately by the
ToolRegistry from Phase 3.
"""

from __future__ import annotations

from app.a2a.errors import TransportAuthError
from app.config import AgentRole
from app.security.jwt_auth import Claims

# Identity subjects
SUBJECT_USER = "user:analyst"


def agent_subject(role: AgentRole) -> str:
    return f"agent:{role.value}"


# Scopes
SCOPE_CREATE_INVESTIGATION = "investigation:create"


def invoke_scope(role: AgentRole) -> str:
    return f"a2a:invoke:{role.value}"


_SPECIALISTS = [AgentRole.KYC, AgentRole.AML, AgentRole.SANCTIONS, AgentRole.FRAUD,
                AgentRole.RISK, AgentRole.REPORTING]

# Least-privilege grant per identity ----------------------------------------
#   orchestrator → may invoke each specialist
#   specialists  → no outbound scopes at all
#   user         → may create an investigation (i.e. invoke the orchestrator)
AGENT_GRANTS: dict[AgentRole, set[str]] = {
    AgentRole.ORCHESTRATOR: {invoke_scope(r) for r in _SPECIALISTS},
    **{r: set() for r in _SPECIALISTS},
}
USER_GRANTS: set[str] = {SCOPE_CREATE_INVESTIGATION}


def required_scope_to_invoke(target: AgentRole) -> str:
    """The scope a caller must hold to invoke ``target``."""
    if target == AgentRole.ORCHESTRATOR:
        return SCOPE_CREATE_INVESTIGATION
    return invoke_scope(target)


def authorize(claims: Claims, target: AgentRole) -> None:
    """Raise TransportAuthError(403) unless ``claims`` may invoke ``target``."""
    needed = required_scope_to_invoke(target)
    if not claims.has_scope(needed):
        raise TransportAuthError(
            403, f"identity '{claims.sub}' lacks scope '{needed}' to invoke {target.value}")
