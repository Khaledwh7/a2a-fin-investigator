"""The authenticator injected into each agent's A2A server.

Given the raw request headers it: (1) extracts the bearer token, (2) verifies it
(authentication), (3) checks the caller holds the scope to invoke *this* agent
(authorization), and (4) writes an audit entry either way. It returns the caller
identity (``sub``) or raises ``TransportAuthError`` — which the server maps to a
401/403. The A2A layer never imports this module; the factory injects it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.a2a.errors import TransportAuthError
from app.config import AgentRole
from app.security.audit import AuditLogger
from app.security.jwt_auth import TokenService, extract_bearer
from app.security.rbac import authorize

# The shape the A2A server expects: headers -> caller identity (or None).
Authenticator = Callable[[dict[str, str]], Awaitable["str | None"]]


def make_authenticator(*, token_service: TokenService, target: AgentRole,
                       audit: AuditLogger, enabled: bool) -> Authenticator:
    async def authenticate(headers: dict[str, str]) -> str | None:
        if not enabled:
            return None  # auth disabled → open (dev/tests)

        token = extract_bearer(headers)
        if token is None:
            await audit.record(actor="anonymous", action="invoke",
                               resource=target.value, outcome="denied:no_token")
            raise TransportAuthError(401, "missing bearer token")

        try:
            claims = token_service.verify(token)          # authentication
            authorize(claims, target)                     # authorization (RBAC)
        except TransportAuthError as exc:
            actor = "unknown"
            try:
                actor = token_service.verify(token).sub
            except TransportAuthError:
                pass
            await audit.record(actor=actor, action="invoke", resource=target.value,
                               outcome=f"denied:{exc.status_code}")
            raise

        await audit.record(actor=claims.sub, action="invoke", resource=target.value,
                           outcome="allowed")
        return claims.sub

    return authenticate
