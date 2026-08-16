"""JWT — Authentication: *who are you?*

A bearer JWT is how every caller (an agent, or the human analyst) proves its
identity. We sign with HS256 and a shared secret for the demo; in production
you'd use RS256 with a real Identity Provider (Keycloak/Auth0/Entra) and rotate
keys. The claims we care about:

    sub    — the identity (e.g. "agent:orchestrator" or "user:analyst")
    scope  — space-delimited permissions (this is the Authorization half — RBAC)
    iss    — issuer we trust
    aud    — audience (this system)
    exp    — expiry (short-lived: 15 min)
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import jwt

from app.a2a.errors import TransportAuthError

_AUDIENCE = "a2a-fin-investigator"


@dataclass(frozen=True)
class Claims:
    sub: str
    scopes: frozenset[str]
    raw: dict

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class TokenService:
    def __init__(self, secret: str, issuer: str, algorithm: str = "HS256",
                 ttl_seconds: int = 900) -> None:
        self._secret = secret
        self._issuer = issuer
        self._alg = algorithm
        self._ttl = ttl_seconds

    def issue(self, subject: str, scopes: set[str], ttl_seconds: int | None = None) -> str:
        now = int(time.time())
        payload = {
            "sub": subject,
            "scope": " ".join(sorted(scopes)),
            "iss": self._issuer,
            "aud": _AUDIENCE,
            "iat": now,
            "exp": now + (ttl_seconds or self._ttl),
        }
        return jwt.encode(payload, self._secret, algorithm=self._alg)

    def verify(self, token: str) -> Claims:
        """Decode + validate. Raises TransportAuthError(401) on any problem."""
        try:
            payload = jwt.decode(
                token, self._secret, algorithms=[self._alg],
                audience=_AUDIENCE, issuer=self._issuer,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TransportAuthError(401, "token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise TransportAuthError(401, f"invalid token: {exc}") from exc
        scopes = frozenset((payload.get("scope") or "").split())
        return Claims(sub=payload["sub"], scopes=scopes, raw=payload)


def extract_bearer(headers: dict[str, str]) -> str | None:
    """Pull the token out of an ``Authorization: Bearer <token>`` header."""
    value = headers.get("authorization") or headers.get("Authorization")
    if not value:
        return None
    parts = value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()
