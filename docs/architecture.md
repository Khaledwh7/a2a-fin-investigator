# Architecture — AI Financial Investigation Assistant (A2A)

> **Phase 1 deliverable.** This document defines the system before any feature
> code is written. It is also the map you use to explain the project in an
> interview.

---

## 1. What this project is

A multi-agent system that runs a **financial crime investigation** on a
customer scenario. Six specialized agents collaborate over the **Agent2Agent
(A2A) protocol** to produce a single investigation report with a risk score.

```
                 User (Streamlit UI)
                        │  "Investigate customer X"
                        ▼
              ┌───────────────────┐
              │  Orchestrator     │  plans + routes, owns the root Task
              └───────────────────┘
        A2A  │      A2A │     A2A │      A2A │        A2A │
             ▼          ▼         ▼          ▼            ▼
          ┌─────┐   ┌─────┐  ┌──────────┐ ┌──────┐  ┌───────────┐
          │ KYC │→  │ AML │→ │Sanctions │→│ Risk │→ │ Reporting │
          └─────┘   └─────┘  └──────────┘ └──────┘  └───────────┘
             │         │          │          │            │
             └─────────┴── Artifacts + shared contextId ──┘
                        │
                        ▼
              Final Investigation Report (Artifact)
```

The investigation is deliberately **linear** (KYC → AML → Sanctions → Risk →
Reporting) because that mirrors how a real case is triaged, and because a clear
pipeline is easy to visualize and easy to explain. The Orchestrator is what
makes it A2A: it discovers each agent, opens an A2A task with it, passes
context forward, and collects artifacts back.

---

## 2. Design principles (what keeps it portfolio-sized)

| Principle | Consequence |
|---|---|
| **Spec-accurate, not spec-exhaustive** | We implement the A2A concepts that matter (Card, discovery, Message/Part, Task lifecycle, Artifact, Context, streaming, errors) and skip the rest (gRPC binding, push-notification configs, extended card). |
| **A2A implemented from scratch** | No third-party A2A SDK. ~1 file of protocol types + a tiny JSON-RPC server/client. This is a *choice*: it means every field on the wire is one I can explain, which is the point of a learning project. |
| **One process, real HTTP between agents** | All six agents are mounted in one FastAPI app, each at its own URL. They still call each other over **real HTTP A2A requests** (loopback), so discovery, JSON-RPC, auth headers and streaming are genuinely exercised. Splitting into six containers later is only a change of peer URLs. |
| **Deterministic core, optional LLM** | Each agent's domain logic is rule-based and seeded, so the demo is reproducible and needs no API key. An optional Claude layer adds natural-language reasoning when a key is present. |
| **SQLite by default** | Zero-config persistence for tasks and the audit log. `docker-compose` is just two services: `app` + `ui`. |

---

## 3. Folder structure

```
a2a-fin-investigator/
├── app/
│   ├── config.py                 # all settings, loaded once from env
│   ├── a2a/                       # THE PROTOCOL — reusable, domain-agnostic
│   │   ├── types.py               #   spec-accurate Message/Part/Task/Artifact/AgentCard
│   │   ├── errors.py              #   A2A JSON-RPC error codes (-32001 … -32009)
│   │   ├── agent_card.py          #   build + serve /.well-known/agent-card.json
│   │   ├── executor.py            #   AgentExecutor base class (how an agent runs a task)
│   │   ├── task_store.py          #   Task lifecycle + persistence
│   │   ├── server.py              #   JSON-RPC dispatch (SendMessage, GetTask, …) + SSE
│   │   └── client.py              #   A2A client: discover a peer, send, stream, retry
│   ├── agents/                    # THE DOMAIN — one executor per role
│   │   ├── orchestrator.py        #   plans the investigation, calls the five specialists
│   │   ├── kyc.py                 #   identity & document checks
│   │   ├── aml.py                 #   transaction / money-laundering signals
│   │   ├── sanctions.py           #   watchlist screening (OFAC-style mock)
│   │   ├── risk.py                #   aggregates signals into a risk score
│   │   └── reporting.py           #   writes the final report artifact
│   ├── security/                  # auth, RBAC, validation, audit, injection defense
│   ├── tools/                     # the mock data sources agents are allowed to call
│   ├── database/                  # SQLAlchemy models + session
│   ├── observability/             # structured logs, traces, latency/token/cost metrics
│   ├── evaluation/                # scores a finished investigation
│   └── api/                       # FastAPI app factory, agent mounting, human-facing routes
├── ui/                            # Streamlit app + self-contained HTML/SVG components
├── tests/                         # pytest (47 tests)
├── docs/                          # this file + a2a-spec-mapping.md
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

The split that matters: **`app/a2a/` knows nothing about finance, and
`app/agents/` knows nothing about JSON-RPC.** An agent is just an
`AgentExecutor` that receives a `Message` and emits status updates +
`Artifact`s. That boundary is what lets you say "the A2A layer is reusable" in
an interview and mean it.

---

## 4. A2A concepts → where they live

| A2A concept (official) | Implemented in | Note |
|---|---|---|
| **Agent Card** | `a2a/agent_card.py` | one per agent, served at the well-known URI |
| **Agent Discovery** | `a2a/client.py` | fetch a peer's card from `/.well-known/agent-card.json` |
| **A2A communication** | `a2a/server.py` + `client.py` | JSON-RPC 2.0 over HTTP(S) |
| **Message / Parts** | `a2a/types.py` | `Message.parts[]`, each a text / data / file part |
| **Task + lifecycle** | `a2a/task_store.py` | states `SUBMITTED → WORKING → COMPLETED/FAILED/CANCELED` |
| **Context** | `contextId` on every message/task | one id threads the whole investigation |
| **Artifacts** | `a2a/types.py` + each agent | each specialist returns a findings artifact |
| **Streaming / progress** | `a2a/server.py` (SSE) | `TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent` |
| **Error handling** | `a2a/errors.py` | typed A2A error codes mapped to JSON-RPC |

Full field-level mapping (and what we deliberately left out) is in
[`a2a-spec-mapping.md`](./a2a-spec-mapping.md), written in Phase 2.

---

## 5. Which A2A version we target — and why it matters

The official spec is now **A2A v1.0** (Linux Foundation, 2026). It changed
several things that older tutorials still get wrong, so the project targets 1.0
as the source of truth:

| | Pre-1.0 tutorials (v0.x) | **v1.0 — what we build** |
|---|---|---|
| Card URI | `/.well-known/agent.json` | `/.well-known/agent-card.json` |
| RPC method name | `message/send` | `SendMessage` (PascalCase) |
| Task state value | `"working"` | `"TASK_STATE_WORKING"` |
| Card transport | `url` + `preferredTransport` | `supportedInterfaces[]` |
| Version negotiation | — | `A2A-Version` header; error `-32009` |

We keep a small compatibility shim that also accepts the v0.3 method aliases,
so the server interoperates with older clients — a nice thing to point at in an
interview, but not the default path.

**Honesty note for the reader:** A2A concepts (Card, Task, Message, Part,
Artifact, the JSON-RPC methods, the error codes, the well-known URI) are
*official*. The six-agent finance pipeline, the risk scoring, the single-process
hosting, and the security/observability/eval layers are *our implementation
choices*. Every doc keeps that line visible.

---

## 6. Request lifecycle (one investigation, end to end)

```
1. UI  → POST /investigations                 (human REST endpoint, JWT-authed)
2. API → Orchestrator.execute(Message)        creates root Task, contextId = C
3. Orchestrator, for each specialist:
     a. discover peer card  (GET /.well-known/agent-card.json)
     b. A2A SendMessage     (JSON-RPC, Authorization: Bearer <agent JWT>, contextId = C)
     c. receive Task → poll/stream to COMPLETED → collect Artifact
4. Reporting agent composes the final report Artifact from all findings
5. API returns the report; every hop was traced (latency, tokens, cost, status)
6. Evaluation scores the finished run
```

Reliability rails on step 3: per-call **timeout**, **retry with backoff**,
**max-iteration / max-depth** guards so an agent can never loop forever.

---

## 7. Security posture (portfolio-appropriate)

- **Authentication — "who are you?"** Every A2A call and every human call
  carries a **JWT bearer token**. Each agent has its own identity; the
  Orchestrator's token is minted for it, not shared.
- **Authorization — "what are you allowed to do?"** **RBAC**: the token's scope
  says which agent role it may act as and which tools it may call. The Risk
  agent cannot call the Sanctions tool; the KYC agent cannot write the report.
  Least privilege by construction.
- **Secure comms:** TLS in front (compose/reverse-proxy), bearer tokens on every
  hop, and a **peer allowlist** — an agent will only talk to URLs in its
  registry.
- **Rogue-agent protection:** Agent Cards can be **JWS-signed**; a peer whose
  card fails verification is refused.
- **Input validation, rate limiting, prompt-injection filtering, secrets via
  env, and an append-only hash-chained audit log** round it out. Full details in
  the [README security section](../README.md#security-authentication--authorization).

---

## 8. Observability & evaluation (lightweight)

- **Per-hop trace**: agent, task id, status, latency, token usage, estimated
  cost, error — collected in-process and surfaced to the UI. No Prometheus/OTel
  Collector; a small in-app store is enough to show the concepts and render the
  flow.
- **Evaluation**: after a run, score task success, agent routing correctness,
  report quality, factual consistency (does the report match the findings?),
  latency and cost. Details in Phase 7.

---

## 9. Tech decisions, one line each

| Decision | Why |
|---|---|
| FastAPI | async, first-class JSON-RPC handling, easy SSE |
| A2A from scratch | it's a *learning* project — own every wire field |
| Simple orchestration (not LangGraph) | the flow is linear; explicit code is easier to explain and debug than a graph engine |
| SQLite + SQLAlchemy async | zero-config, still shows real persistence + task store |
| PyJWT | standard, supports HS256 and RS256 agent identity |
| Streamlit | fastest path to a polished, demo-able UI |
| Deterministic mocks + optional Claude | reproducible demo, no key required; LLM optional |
```
