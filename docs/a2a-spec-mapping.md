# A2A v1.0 — spec mapping (official vs. our choices)

This is the honesty ledger. It states exactly which parts of the code are the
**official A2A protocol** and which are **our implementation choices**, so the
line never blurs. Source of truth: the A2A v1.0 specification and its
Protocol Buffer definition (Linux Foundation, 2026).

---

## 1. Objects we implement — OFFICIAL

All field names below are the ProtoJSON wire names (camelCase). Enum values are
the normative SCREAMING_SNAKE strings. Implemented in `app/a2a/types.py`.

| Object | Fields we carry | Fields we omit (and why) |
|---|---|---|
| **Message** | `messageId`, `role`, `parts`, `contextId`, `taskId`, `referenceTaskIds`, `metadata` | `extensions` (no A2A extensions used) |
| **Part** | oneof `text` \| `data` \| `raw` \| `url`, plus `filename`, `mediaType`, `metadata` | — (full oneof modelled) |
| **Task** | `id`, `contextId`, `status`, `artifacts`, `history`, `metadata` | — |
| **TaskStatus** | `state`, `message`, `timestamp` | — |
| **Artifact** | `artifactId`, `name`, `description`, `parts`, `metadata` | `extensions` |
| **TaskStatusUpdateEvent** | `taskId`, `contextId`, `status`, `metadata` | — |
| **TaskArtifactUpdateEvent** | `taskId`, `contextId`, `artifact`, `append`, `lastChunk`, `metadata` | — |
| **AgentCard** | `name`, `description`, `version`, `supportedInterfaces`, `provider`, `capabilities`, `defaultInputModes`, `defaultOutputModes`, `skills`, `signatures`, `iconUrl` | `securitySchemes`/`securityRequirements` are added in Phase 5 |
| **AgentInterface** | `url`, `protocolBinding`, `protocolVersion` | `tenant` (no multi-tenancy) |
| **AgentCapabilities** | `streaming`, `pushNotifications`, `extendedAgentCard` | — |
| **AgentSkill** | `id`, `name`, `description`, `tags`, `examples`, `inputModes`, `outputModes` | `securityRequirements` |

## 2. Enums — OFFICIAL (values are normative)

| Enum | Values used |
|---|---|
| `Role` | `ROLE_UNSPECIFIED`, `ROLE_USER`, `ROLE_AGENT` |
| `TaskState` | `TASK_STATE_SUBMITTED`, `TASK_STATE_WORKING`, `TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`, `TASK_STATE_CANCELED`, `TASK_STATE_INPUT_REQUIRED`, `TASK_STATE_REJECTED`, `TASK_STATE_AUTH_REQUIRED` (+`UNSPECIFIED`) |

## 3. JSON-RPC methods — OFFICIAL (v1.0 PascalCase)

Implemented in `app/a2a/server.py`; names in `app/a2a/types.py::Method`.

| Method | Status | Notes |
|---|---|---|
| `SendMessage` | ✅ implemented | returns the final `Task` |
| `SendStreamingMessage` | ✅ implemented | SSE stream of `task` / `statusUpdate` / `artifactUpdate` frames |
| `GetTask` | ✅ implemented | |
| `CancelTask` | ✅ implemented | |
| `SubscribeToTask` | ⛔ returns `UnsupportedOperation` (-32004) | resubscribe not needed for a linear demo |
| `ListTasks` | ⛔ returns `UnsupportedOperation` | not needed by the flow |
| `Create/Get/List/Delete TaskPushNotificationConfig` | ⛔ not implemented | push notifications out of scope |
| `GetExtendedAgentCard` | ⛔ not implemented | single public card is enough |

We additionally accept the **v0.3 aliases** (`message/send`, `tasks/get`, …)
and map them to the v1.0 names, so a legacy client interoperates.

## 4. Error codes — OFFICIAL

`app/a2a/errors.py`. Standard JSON-RPC (`-32700 … -32603`) plus the A2A block:

| Code | Name | Used |
|---|---|---|
| -32001 | TaskNotFound | ✅ GetTask/CancelTask on unknown id |
| -32002 | TaskNotCancelable | ✅ CancelTask on a terminal task |
| -32004 | UnsupportedOperation | ✅ ListTasks/SubscribeToTask |
| -32005 | ContentTypeNotSupported | available |
| -32006 | InvalidAgentResponse | available |
| -32009 | VersionNotSupported | ✅ `A2A-Version` mismatch |
| -32003/-32007/-32008 | (push / extended card / extension) | defined, unused |

## 5. Transport & discovery — OFFICIAL concept, our binding choice

- **Binding:** JSON-RPC 2.0 over HTTP(S). (Spec also allows gRPC and HTTP+JSON;
  we implement only JSON-RPC — an allowed choice.)
- **Discovery:** the well-known URI mechanism, `GET /.well-known/agent-card.json`.
  The spec names three mechanisms (well-known URI, curated registry, direct
  configuration); we use the well-known URI for fetching cards and direct
  configuration (the peer registry in `config.py`) as the trust allowlist.
- **Version negotiation:** the `A2A-Version` request header (spec §3.6.1).

## 5b. Security — OFFICIAL concepts vs. our implementation

| Concept | Official in A2A? | What we do |
|---|---|---|
| `AgentCard.securitySchemes` / `security` | ✅ official fields | We declare an HTTP `bearer`/`JWT` scheme when `REQUIRE_AGENT_AUTH=true` (a faithful subset of the SecurityScheme union) |
| Authentication is transport-level (HTTP 401) | ✅ official stance | Auth failures return HTTP 401/403 with `WWW-Authenticate`, not a JSON-RPC error |
| Signed Agent Cards (`signatures[]`, JWS) | ✅ official v1.0 feature | We attach an HMAC-SHA256 detached-JWS signature; client verifies on discovery. Canonicalization is `json.dumps(sort_keys)` — a **simplification** of the spec's RFC 8785 |
| The specific scopes (`a2a:invoke:kyc`, `investigation:create`) | ❌ ours | Our RBAC scope names |
| JWT as the token, HS256, our claim shape | ❌ ours (A2A is auth-scheme-agnostic) | Our choice; prod would use RS256 + a real IdP |
| RBAC policy / least-privilege matrix | ❌ ours | `app/security/rbac.py` |
| Rate limiting, input validation, prompt-injection guard, audit log | ❌ ours | Standard app-security controls, not A2A concepts |

## 6. Purely OUR choices (not part of A2A)

These exist to make a runnable, explainable portfolio project. A2A says nothing
about any of them:

- The six-agent **finance pipeline** and its domain logic (KYC/AML/…); risk scoring.
- Hosting all agents in **one process** with path-scoped well-known URIs.
- The **AgentExecutor / EventQueue** internal shape (inspired by the official
  Python SDK, but our own code).
- **Persistence** (SQLite task store), **auth** (JWT/RBAC), **observability**
  (latency/token/cost), **evaluation**, and the **Streamlit UI**.

If a reviewer asks "is X part of A2A?", this table answers it.
