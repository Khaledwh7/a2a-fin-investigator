"""Central configuration — the single place the outside world enters the process.

Nothing else reads ``os.environ`` directly. That rule keeps the security review
small: there is exactly one door for untrusted configuration.
"""

from __future__ import annotations

import functools
from enum import StrEnum

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentRole(StrEnum):
    """The agents in the investigation network.

    One process hosts them all (portfolio-scale), but each keeps its own identity,
    Agent Card and URL, so the A2A traffic between them is real HTTP.
    """

    ORCHESTRATOR = "orchestrator"
    KYC = "kyc"
    AML = "aml"
    SANCTIONS = "sanctions"
    FRAUD = "fraud"
    RISK = "risk"
    REPORTING = "reporting"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- app identity -----------------------------------------------------
    environment: str = "development"
    # Host the six agents are reachable at. In single-process mode this is the
    # one FastAPI server; in a multi-container split you override the per-role
    # URLs below.
    public_base_url: str = "http://localhost:8000"

    # --- A2A protocol -----------------------------------------------------
    a2a_protocol_version: str = "1.0"
    # Also accept clients that declare "0.3" and use legacy method aliases
    # (message/send, tasks/get, ...). Handy for interop; turn off once all
    # peers are on 1.0.
    a2a_accept_legacy_v03: bool = True

    # --- datastore --------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./data/investigations.db"

    # --- peer registry (static discovery + trust allowlist) ---------------
    # A role not in this map cannot be called by us and cannot call us.
    orchestrator_url: str = "http://localhost:8000/a2a/orchestrator"
    kyc_url: str = "http://localhost:8000/a2a/kyc"
    aml_url: str = "http://localhost:8000/a2a/aml"
    sanctions_url: str = "http://localhost:8000/a2a/sanctions"
    fraud_url: str = "http://localhost:8000/a2a/fraud"
    risk_url: str = "http://localhost:8000/a2a/risk"
    reporting_url: str = "http://localhost:8000/a2a/reporting"

    # --- auth (used from Phase 5) -----------------------------------------
    # Dev default is ≥32 bytes (RFC 7518 minimum for HS256). Override in prod.
    jwt_secret: SecretStr = SecretStr("dev-only-change-me-32byte-minimum-secret!")
    jwt_issuer: str = "a2a-fin-investigator"
    jwt_algorithm: str = "HS256"
    jwt_key_id: str = "card-key-1"
    access_token_ttl_seconds: int = 900
    require_agent_auth: bool = False  # flipped on in Phase 5
    require_signed_agent_cards: bool = False

    # --- human-in-the-loop ------------------------------------------------
    # When on, high-stakes outcomes (HIGH/CRITICAL, SAR, or a sanctions hit)
    # PAUSE at A2A TASK_STATE_INPUT_REQUIRED for an analyst to approve / override
    # / close before the report is finalised. Low/medium cases auto-complete.
    require_human_review: bool = True

    # --- reliability ------------------------------------------------------
    max_agent_iterations: int = 6
    max_delegation_depth: int = 3
    task_timeout_seconds: float = 60.0
    peer_request_timeout_seconds: float = 30.0
    peer_connect_timeout_seconds: float = 5.0
    retry_max_attempts: int = 3
    retry_base_delay_seconds: float = 0.25
    retry_max_delay_seconds: float = 5.0

    # --- limits / validation ---------------------------------------------
    rate_limit_enabled: bool = False  # on in docker/prod; off for tests by default
    rate_limit_requests_per_minute: int = 120
    rate_limit_burst: int = 30
    max_request_bytes: int = 262_144
    max_parts_per_message: int = 32
    max_text_part_chars: int = 20_000

    # --- observability ----------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "json"

    # --- LLM (optional) ---------------------------------------------------
    # Runs fully deterministic with llm_enabled=false — no API key needed.
    llm_enabled: bool = False
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5"
    llm_effort: str = "medium"
    llm_max_tokens: int = 4096

    # --- TLS (outbound peer verification) ---------------------------------
    tls_verify: bool = True
    tls_ca_bundle: str = ""

    @field_validator(
        "public_base_url", "orchestrator_url", "kyc_url", "aml_url",
        "sanctions_url", "fraud_url", "risk_url", "reporting_url",
    )
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def peer_urls(self) -> dict[AgentRole, str]:
        return {
            AgentRole.ORCHESTRATOR: self.orchestrator_url,
            AgentRole.KYC: self.kyc_url,
            AgentRole.AML: self.aml_url,
            AgentRole.SANCTIONS: self.sanctions_url,
            AgentRole.FRAUD: self.fraud_url,
            AgentRole.RISK: self.risk_url,
            AgentRole.REPORTING: self.reporting_url,
        }

    @property
    def httpx_verify(self) -> str | bool:
        return self.tls_ca_bundle or self.tls_verify


@functools.lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton. Tests call ``get_settings.cache_clear()``."""
    return Settings()
