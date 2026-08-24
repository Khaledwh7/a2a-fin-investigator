"""A2A v1.0 data model — OFFICIAL protocol types.

Every model here mirrors the A2A v1.0 Protocol Buffer / JSON schema. The wire
format follows ProtoJSON, which means two rules we honour throughout:

  1. **Field names are camelCase**  (e.g. ``contextId``, ``mediaType``).
  2. **Enum values are SCREAMING_SNAKE_CASE with a type prefix**
     (e.g. ``"TASK_STATE_WORKING"``, ``"ROLE_AGENT"``).

We use Pydantic with an alias generator so Python code stays snake_case while
the JSON on the wire is spec-exact. Serialize with
``model_dump(by_alias=True, exclude_none=True)``.

What we implement vs. omit is documented in docs/a2a-spec-mapping.md. In short:
we model Message/Part/Task/TaskStatus/Artifact/AgentCard and the streaming
events; we omit multi-tenancy (`tenant`), push-notification configs and the
gRPC binding, none of which a portfolio demo needs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


def _now_iso() -> str:
    """RFC 3339 / ISO 8601 UTC timestamp, as the spec's Timestamp expects."""
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class _A2AModel(BaseModel):
    """Base: camelCase on the wire, snake_case in Python, accept both on input."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",  # tolerate unknown fields from newer/older peers
    )

    def to_wire(self) -> dict[str, Any]:
        """Serialize to a spec-shaped JSON dict."""
        return self.model_dump(by_alias=True, exclude_none=True)


# ---------------------------------------------------------------------------
# Enums  (OFFICIAL — values are normative ProtoJSON strings)
# ---------------------------------------------------------------------------
class Role(StrEnum):
    UNSPECIFIED = "ROLE_UNSPECIFIED"
    USER = "ROLE_USER"
    AGENT = "ROLE_AGENT"


class TaskState(StrEnum):
    UNSPECIFIED = "TASK_STATE_UNSPECIFIED"
    SUBMITTED = "TASK_STATE_SUBMITTED"
    WORKING = "TASK_STATE_WORKING"
    COMPLETED = "TASK_STATE_COMPLETED"
    FAILED = "TASK_STATE_FAILED"
    CANCELED = "TASK_STATE_CANCELED"
    INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    REJECTED = "TASK_STATE_REJECTED"
    AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELED,
            TaskState.REJECTED,
        }


# ---------------------------------------------------------------------------
# Part  (OFFICIAL — a oneof of text / data / file content)
# ---------------------------------------------------------------------------
class Part(_A2AModel):
    """A piece of content inside a Message or Artifact.

    In the proto, ``Part`` is a ``oneof`` over ``text | data | raw | url``.
    ProtoJSON encodes the choice by *which field is present*, so we model all
    four as optional and validate that exactly one is set.

      - text : plain text
      - data : structured JSON blob (dict/list/scalar)
      - raw  : inline file bytes, base64 (paired with filename/mediaType)
      - url  : pointer to file content (paired with filename/mediaType)
    """

    text: str | None = None
    data: Any | None = None
    raw: str | None = None            # base64-encoded bytes
    url: str | None = None
    filename: str | None = None
    media_type: str | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one_content(self) -> Part:
        present = [f for f in ("text", "data", "raw", "url")
                   if getattr(self, f) is not None]
        if len(present) != 1:
            raise ValueError(
                f"a Part must carry exactly one of text/data/raw/url, got {present or 'none'}"
            )
        return self

    @property
    def kind(self) -> Literal["text", "data", "file"]:
        if self.text is not None:
            return "text"
        if self.data is not None:
            return "data"
        return "file"  # raw or url

    # --- ergonomic constructors ---------------------------------------
    @classmethod
    def from_text(cls, text: str, **meta: Any) -> Part:
        return cls(text=text, metadata=meta or None)

    @classmethod
    def from_data(cls, data: Any, **meta: Any) -> Part:
        return cls(data=data, metadata=meta or None)

    @classmethod
    def from_uri(cls, url: str, filename: str | None = None,
                 media_type: str | None = None) -> Part:
        return cls(url=url, filename=filename, media_type=media_type)


# ---------------------------------------------------------------------------
# Message  (OFFICIAL)
# ---------------------------------------------------------------------------
class Message(_A2AModel):
    """A single turn between a user and an agent, or between two agents."""

    message_id: str = Field(default_factory=lambda: _new_id("msg"))
    role: Role = Role.USER
    parts: list[Part] = Field(default_factory=list)
    context_id: str | None = None
    task_id: str | None = None
    reference_task_ids: list[str] | None = None
    metadata: dict[str, Any] | None = None

    # --- constructors --------------------------------------------------
    @classmethod
    def user_text(cls, text: str, *, context_id: str | None = None,
                  task_id: str | None = None) -> Message:
        return cls(role=Role.USER, parts=[Part.from_text(text)],
                   context_id=context_id, task_id=task_id)

    @classmethod
    def agent_parts(cls, parts: list[Part], *, context_id: str | None = None,
                    task_id: str | None = None) -> Message:
        return cls(role=Role.AGENT, parts=parts, context_id=context_id, task_id=task_id)

    @classmethod
    def agent_text(cls, text: str, *, context_id: str | None = None,
                   task_id: str | None = None) -> Message:
        return cls(role=Role.AGENT, parts=[Part.from_text(text)],
                   context_id=context_id, task_id=task_id)

    # --- helpers -------------------------------------------------------
    @property
    def text(self) -> str:
        """Concatenate all text parts — the common case for a prompt."""
        return "\n".join(p.text for p in self.parts if p.text is not None)

    def first_data(self) -> Any | None:
        for p in self.parts:
            if p.data is not None:
                return p.data
        return None


# ---------------------------------------------------------------------------
# Artifact  (OFFICIAL — an agent's tangible output)
# ---------------------------------------------------------------------------
class Artifact(_A2AModel):
    artifact_id: str = Field(default_factory=lambda: _new_id("artifact"))
    name: str | None = None
    description: str | None = None
    parts: list[Part] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None

    @classmethod
    def of_data(cls, name: str, data: Any, description: str | None = None) -> Artifact:
        return cls(name=name, description=description, parts=[Part.from_data(data)])

    @classmethod
    def of_text(cls, name: str, text: str, description: str | None = None) -> Artifact:
        return cls(name=name, description=description, parts=[Part.from_text(text)])

    def first_data(self) -> Any | None:
        for p in self.parts:
            if p.data is not None:
                return p.data
        return None


# ---------------------------------------------------------------------------
# Task + TaskStatus  (OFFICIAL — the unit of work and its lifecycle)
# ---------------------------------------------------------------------------
class TaskStatus(_A2AModel):
    state: TaskState = TaskState.SUBMITTED
    message: Message | None = None      # optional human/agent-readable status note
    timestamp: str = Field(default_factory=_now_iso)


class Task(_A2AModel):
    id: str = Field(default_factory=lambda: _new_id("task"))
    context_id: str = Field(default_factory=lambda: _new_id("ctx"))
    status: TaskStatus = Field(default_factory=TaskStatus)
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Streaming events  (OFFICIAL — delivered over SSE for the *streaming methods)
# ---------------------------------------------------------------------------
class TaskStatusUpdateEvent(_A2AModel):
    task_id: str
    context_id: str
    status: TaskStatus
    metadata: dict[str, Any] | None = None


class TaskArtifactUpdateEvent(_A2AModel):
    task_id: str
    context_id: str
    artifact: Artifact
    append: bool = False
    last_chunk: bool = True
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# JSON-RPC method names  (OFFICIAL — v1.0 PascalCase, with v0.3 aliases)
# ---------------------------------------------------------------------------
class Method:
    """Canonical v1.0 method-name strings sent in the JSON-RPC ``method`` field."""

    SEND_MESSAGE = "SendMessage"
    SEND_STREAMING_MESSAGE = "SendStreamingMessage"
    GET_TASK = "GetTask"
    LIST_TASKS = "ListTasks"
    CANCEL_TASK = "CancelTask"
    SUBSCRIBE_TO_TASK = "SubscribeToTask"
    GET_EXTENDED_AGENT_CARD = "GetExtendedAgentCard"


# v0.3 wire names → v1.0 canonical. Lets a 1.0 server accept legacy clients.
LEGACY_METHOD_ALIASES: dict[str, str] = {
    "message/send": Method.SEND_MESSAGE,
    "message/stream": Method.SEND_STREAMING_MESSAGE,
    "tasks/get": Method.GET_TASK,
    "tasks/list": Method.LIST_TASKS,
    "tasks/cancel": Method.CANCEL_TASK,
    "tasks/resubscribe": Method.SUBSCRIBE_TO_TASK,
}


def canonical_method(name: str) -> str:
    """Map any accepted alias to its canonical v1.0 name."""
    return LEGACY_METHOD_ALIASES.get(name, name)


# ---------------------------------------------------------------------------
# Agent Card + sub-objects  (OFFICIAL — the agent's public "business card")
# ---------------------------------------------------------------------------
class AgentProvider(_A2AModel):
    organization: str
    url: str | None = None


class AgentInterface(_A2AModel):
    """One way to reach the agent. ``url`` is the JSON-RPC endpoint."""

    url: str
    protocol_binding: str = "JSONRPC"      # officially: JSONRPC | GRPC | HTTP+JSON
    protocol_version: str = "1.0"


class AgentCapabilities(_A2AModel):
    streaming: bool = True
    push_notifications: bool = False
    extended_agent_card: bool = False


class AgentSkill(_A2AModel):
    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] | None = None
    input_modes: list[str] | None = None
    output_modes: list[str] | None = None


class AgentCardSignature(_A2AModel):
    """Detached JWS over the card (rogue-agent protection)."""

    protected: str
    signature: str
    header: dict[str, Any] | None = None


class AgentCard(_A2AModel):
    name: str
    description: str
    version: str
    supported_interfaces: list[AgentInterface] = Field(default_factory=list)
    provider: AgentProvider | None = None
    documentation_url: str | None = None
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    # OFFICIAL: securitySchemes declares HOW to authenticate; security lists the
    # requirement(s). We model them as plain dicts (a faithful subset of the
    # spec's SecurityScheme union) — see docs/a2a-spec-mapping.md.
    security_schemes: dict[str, Any] | None = None
    security: list[dict[str, list[str]]] | None = None
    default_input_modes: list[str] = Field(default_factory=lambda: ["text/plain",
                                                                     "application/json"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text/plain",
                                                                      "application/json"])
    skills: list[AgentSkill] = Field(default_factory=list)
    signatures: list[AgentCardSignature] | None = None
    icon_url: str | None = None

    @property
    def rpc_url(self) -> str | None:
        """The JSON-RPC endpoint a client should POST to."""
        return self.supported_interfaces[0].url if self.supported_interfaces else None
