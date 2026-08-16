"""System assembly — build the six agents into one secured FastAPI app.

Portfolio hosting model: all six agents live in one process, each mounted at
``/a2a/{role}`` with its own Agent Card and JSON-RPC endpoint. The Orchestrator
reaches the others over real HTTP through an ``A2AClient``.

This is also where **security is composed and injected**. The protocol layer
(``app/a2a``) stays security-agnostic; here we build the token service, RBAC
authenticators, audit log, message validator, rate limiter and (optionally)
signed cards, and wire them into the agents and the outbound client. Which
features are active is driven by settings flags, so the same code runs open
(dev/tests) or locked-down (docker/prod).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.a2a.client import A2AClient
from app.a2a.executor import AgentExecutor
from app.a2a.server import A2AAgent, build_router
from app.a2a.task_store import InMemoryTaskStore, TaskStore
from app.a2a.types import AgentCard
from app.agents.aml import AMLExecutor
from app.agents.cards import build_all_cards
from app.agents.fraud import FraudExecutor
from app.agents.kyc import KYCExecutor
from app.agents.orchestrator import OrchestratorExecutor
from app.agents.reporting import ReportingExecutor
from app.agents.risk import RiskExecutor
from app.agents.sanctions import SanctionsExecutor
from app.api.routes import register_human_api
from app.config import AgentRole, Settings, get_settings
from app.database.repository import InvestigationRepository
from app.database.session import init_models, make_engine, make_session_factory
from app.database.task_store import SqlTaskStore
from app.observability.logging import configure_logging
from app.observability.metrics import Metrics
from app.observability.trace import TraceStore
from app.security.audit import AuditLogger, DbAuditLogger, MemoryAuditLogger
from app.security.authn import make_authenticator
from app.security.jwt_auth import TokenService
from app.security.rate_limit import RateLimiter, install_http_guards
from app.security.rbac import AGENT_GRANTS, agent_subject
from app.security.signing import sign_card, verify_card
from app.security.validation import make_message_validator
from app.tools.finance import build_default_registry


def build_executors(settings: Settings) -> dict[AgentRole, AgentExecutor]:
    registry = build_default_registry()
    return {
        AgentRole.ORCHESTRATOR: OrchestratorExecutor(settings.peer_urls, settings),
        AgentRole.KYC: KYCExecutor(registry),
        AgentRole.AML: AMLExecutor(registry),
        AgentRole.SANCTIONS: SanctionsExecutor(registry),
        AgentRole.FRAUD: FraudExecutor(registry),
        AgentRole.RISK: RiskExecutor(registry),
        AgentRole.REPORTING: ReportingExecutor(registry),
    }


def _prepare_card(card: AgentCard, settings: Settings, secret: str) -> AgentCard:
    """Declare the security scheme (if auth on) and sign the card (if required)."""
    if settings.require_agent_auth:
        card.security_schemes = {
            "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
        }
        card.security = [{"bearerAuth": []}]
    if settings.require_signed_agent_cards:
        card = sign_card(card, secret=secret, key_id=settings.jwt_key_id)
    return card


def default_client(settings: Settings, *, token_service: TokenService | None,
                   secret: str) -> A2AClient:
    """The orchestrator's outbound client, carrying its identity + trust config."""
    token_provider = None
    if settings.require_agent_auth and token_service is not None:
        grants = AGENT_GRANTS[AgentRole.ORCHESTRATOR]
        subject = agent_subject(AgentRole.ORCHESTRATOR)
        # Fresh, short-lived token per call — the orchestrator proving its identity.
        token_provider = lambda _url: token_service.issue(subject, grants)  # noqa: E731

    card_verifier = None
    if settings.require_signed_agent_cards:
        card_verifier = lambda card: verify_card(card, secret=secret)  # noqa: E731

    return A2AClient(
        protocol_version=settings.a2a_protocol_version,
        timeout=settings.peer_request_timeout_seconds,
        connect_timeout=settings.peer_connect_timeout_seconds,
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay_seconds,
        max_delay=settings.retry_max_delay_seconds,
        verify=settings.httpx_verify,
        token_provider=token_provider,
        require_signed_cards=settings.require_signed_agent_cards,
        card_verifier=card_verifier,
    )


def _build_persistence(database_url: str | None, write_lock: asyncio.Lock
                       ) -> tuple[dict[AgentRole, TaskStore], AsyncEngine | None,
                                  async_sessionmaker[AsyncSession] | None,
                                  InvestigationRepository | None]:
    if database_url is None:
        return {role: InMemoryTaskStore() for role in AgentRole}, None, None, None
    engine = make_engine(database_url)
    session_factory = make_session_factory(engine)
    stores: dict[AgentRole, TaskStore] = {
        role: SqlTaskStore(role.value, session_factory, write_lock) for role in AgentRole
    }
    return stores, engine, session_factory, InvestigationRepository(session_factory)


def build_app(settings: Settings | None = None, *, client: A2AClient | None = None,
              database_url: str | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    secret = settings.jwt_secret.get_secret_value()
    write_lock = asyncio.Lock()

    # --- persistence ----------------------------------------------------
    stores, engine, session_factory, investigations = _build_persistence(
        database_url, write_lock)

    # --- security components -------------------------------------------
    token_service = TokenService(secret, settings.jwt_issuer, settings.jwt_algorithm,
                                 settings.access_token_ttl_seconds)
    audit: AuditLogger = (DbAuditLogger(session_factory, write_lock)
                          if session_factory is not None else MemoryAuditLogger())
    validator = make_message_validator(max_parts=settings.max_parts_per_message,
                                       max_text_chars=settings.max_text_part_chars)

    # --- observability --------------------------------------------------
    trace_store = TraceStore()
    metrics = Metrics()

    # --- agents ---------------------------------------------------------
    executors = build_executors(settings)
    cards = build_all_cards(settings.peer_urls)
    orchestrator: OrchestratorExecutor = executors[AgentRole.ORCHESTRATOR]  # type: ignore[assignment]
    orchestrator.audit = audit
    orchestrator.trace_store = trace_store
    orchestrator.metrics = metrics

    agents: dict[AgentRole, A2AAgent] = {}
    for role in AgentRole:
        agents[role] = A2AAgent(
            card=_prepare_card(cards[role], settings, secret),
            executor=executors[role],
            task_store=stores[role],
            supported_version=settings.a2a_protocol_version,
            accept_legacy_v03=settings.a2a_accept_legacy_v03,
            authenticator=make_authenticator(token_service=token_service, target=role,
                                              audit=audit,
                                              enabled=settings.require_agent_auth),
            message_validator=validator,
            task_timeout_seconds=settings.task_timeout_seconds,
        )

    # --- app + lifespan -------------------------------------------------
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if engine is not None:
            await init_models(engine)
        yield
        await orchestrator.aclose()
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="AI Financial Investigation Assistant (A2A)", lifespan=lifespan)
    for role in AgentRole:
        app.include_router(build_router(agents[role]), prefix=f"/a2a/{role.value}")

    # HTTP-level guards: request-size always; rate limit when enabled.
    install_http_guards(
        app, max_bytes=settings.max_request_bytes,
        limiter=RateLimiter(settings.rate_limit_requests_per_minute,
                            settings.rate_limit_burst)
        if settings.rate_limit_enabled else None)

    orchestrator.set_client(
        client or default_client(settings, token_service=token_service, secret=secret))

    app.state.settings = settings
    app.state.agents = agents
    app.state.executors = executors
    app.state.orchestrator = orchestrator
    app.state.db_engine = engine
    app.state.investigations = investigations
    app.state.token_service = token_service
    app.state.audit = audit
    app.state.trace_store = trace_store
    app.state.metrics = metrics

    register_human_api(app)   # POST /investigations, GET /metrics, ...
    return app
