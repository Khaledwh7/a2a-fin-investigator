"""Shared pytest fixtures and A2A wiring helpers.

The app ships **secure by default** — agent-to-agent calls carry a short-lived
JWT and Agent Cards are signed — so the tests wire clients the same way the
factory does. Substituting a bare client would quietly skip both and leave the
security configuration untested, which is how a shipped default drifts away from
the one the suite proves.
"""

from __future__ import annotations

import httpx
import pytest

from app.a2a.client import A2AClient
from app.api.factory import default_client
from app.security.rbac import SUBJECT_USER, USER_GRANTS


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Ensure each test gets fresh settings if it patched the environment."""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def wire_orchestrator(app, transport: httpx.ASGITransport) -> A2AClient:
    """Point the orchestrator at an in-process app, keeping its real credentials.

    Same client the factory builds — token provider, card verification, retry
    policy — with the socket swapped for the ASGI transport.
    """
    settings = app.state.settings
    client = default_client(settings, token_service=app.state.token_service,
                            secret=settings.jwt_secret.get_secret_value(),
                            transport=transport)
    client.set_loopback(transport)
    app.state.orchestrator.set_client(client)
    app.state.a2a_client = client
    return client


def user_client(app, transport: httpx.ASGITransport, *,
                subject: str = SUBJECT_USER,
                grants: set[str] | None = None,
                max_attempts: int = 1) -> A2AClient:
    """A client carrying a human analyst's token (when auth is on)."""
    settings = app.state.settings
    provider = None
    if settings.require_agent_auth:
        token_service = app.state.token_service
        scopes = USER_GRANTS if grants is None else grants
        provider = lambda _url: token_service.issue(subject, scopes)  # noqa: E731
    return A2AClient(transport=transport, max_attempts=max_attempts,
                     token_provider=provider)
