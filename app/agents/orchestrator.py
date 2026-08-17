"""Orchestrator Agent — the conductor of the investigation.

Unlike the specialists, the Orchestrator makes *outbound* A2A calls. For each
specialist it:

    1. discovers the peer's Agent Card  (GET /.well-known/agent-card.json)
    2. sends an A2A message on the shared contextId
    3. waits for the peer's Task to complete and pulls its findings Artifact
    4. re-emits that Artifact on its own Task, so the Orchestrator's final Task
       carries the entire investigation.

Reliability rails baked in:
  * each peer call inherits the A2AClient's timeout + retry-with-backoff;
  * a **delegation-depth** guard (defense-in-depth against agent loops);
  * a **step counter** capped at max_agent_iterations;
  * a failing specialist degrades to empty findings rather than aborting the run.
"""

from __future__ import annotations

import time

from app.a2a.client import A2AClient
from app.a2a.errors import A2AError
from app.a2a.executor import AgentExecutor, EventQueue, RequestContext
from app.a2a.types import AgentCard, Message, Part, TaskState
from app.agents.schemas import parse_profile
from app.config import AgentRole, Settings
from app.observability.cost import cost_usd, estimate_payload_tokens
from app.observability.logging import get_logger, log_event
from app.observability.metrics import Metrics
from app.observability.trace import InvestigationTrace, Span, TraceStore
from app.security.audit import AuditLogger
from app.security.prompt_guard import scan as scan_injection

_log = get_logger("orchestrator")

# The fixed investigation pipeline. Linear + finite → structurally loop-free.
_SPECIALISTS = [AgentRole.KYC, AgentRole.AML, AgentRole.SANCTIONS, AgentRole.FRAUD]

# Band → decision (used when an analyst overrides the recommendation).
_DECISION_FOR = {"CRITICAL": "DECLINE & FILE SAR", "HIGH": "ENHANCED DUE DILIGENCE",
                 "MEDIUM": "STANDARD DUE DILIGENCE", "LOW": "APPROVE"}


def _requires_review(risk: dict, sanctions: dict) -> bool:
    """The 'right case' for a human: any high-stakes, regulated outcome."""
    return (risk.get("risk_band") in {"HIGH", "CRITICAL"}
            or bool(risk.get("sar_recommended"))
            or bool(sanctions.get("hit"))
            or bool(sanctions.get("counterparty_hit")))


class OrchestratorExecutor(AgentExecutor):
    role = "orchestrator"

    def __init__(self, peers: dict[AgentRole, str], settings: Settings) -> None:
        self.peers = peers
        self.settings = settings
        self.client: A2AClient | None = None
        self.audit: AuditLogger | None = None
        self.trace_store: TraceStore | None = None
        self.metrics: Metrics | None = None
        self._cards: dict[AgentRole, AgentCard] = {}

    async def _record(self, actor: str, action: str, resource: str, outcome: str,
                      detail: dict | None = None) -> None:
        if self.audit is not None:
            await self.audit.record(actor=actor, action=action, resource=resource,
                                    outcome=outcome, detail=detail or {})

    def set_client(self, client: A2AClient) -> None:
        self.client = client

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def run(self, ctx: RequestContext, events: EventQueue) -> None:
        if self.client is None:
            raise RuntimeError("orchestrator client not wired; call set_client()")

        # --- human-in-the-loop resume ------------------------------------
        # A follow-up message carrying an analyst decision resumes a task that
        # was paused at INPUT_REQUIRED (see _finalise). We reconstruct the case
        # from the paused task's artifacts rather than re-running the specialists.
        human_decision = (ctx.message.first_data() or {}).get("human_decision")
        if human_decision is not None:
            await self._resume(ctx, events, human_decision)
            return

        # --- guard against runaway delegation chains ---------------------
        depth = int((ctx.message.metadata or {}).get("delegation_depth", 0))
        if depth >= self.settings.max_delegation_depth:
            await events.update_status(ctx.task, TaskState.REJECTED,
                                       "max delegation depth exceeded")
            return

        await events.update_status(ctx.task, TaskState.WORKING, "planning investigation")

        payload = ctx.message.first_data() or {}
        profile = parse_profile(payload.get("profile", payload))
        context_id = ctx.task.context_id  # shared across every downstream agent
        caller = ctx.caller or "anonymous"
        findings: dict[str, dict] = {}
        failures: list[str] = []
        steps = 0

        trace = InvestigationTrace(context_id=context_id, task_id=ctx.task.id,
                                   customer=profile.full_name)
        wall0 = time.perf_counter()
        log_event(_log, "investigation_started", context_id=context_id,
                  customer=profile.full_name, caller=caller)

        # --- prompt-injection detection (defense-in-depth) --------------
        labels = sorted(set(scan_injection(profile.notes) + scan_injection(profile.full_name)))
        if labels:
            await self._record(caller, "prompt_injection_detected", profile.full_name,
                               "flagged", {"labels": labels})
            await events.update_status(
                ctx.task, TaskState.WORKING,
                f"⚠ prompt-injection patterns detected and neutralized: {labels}")

        await self._record(caller, "investigation_started", profile.full_name, "ok",
                           {"context_id": context_id})

        try:
            # --- KYC → AML → Sanctions ----------------------------------
            for role in _SPECIALISTS:
                steps += 1
                if steps > self.settings.max_agent_iterations:
                    await events.update_status(ctx.task, TaskState.FAILED,
                                               "exceeded max agent iterations")
                    return
                findings[role.value] = await self._delegate(
                    events, ctx, role, {"profile": profile.model_dump()},
                    context_id, depth, failures, trace)

            # --- Risk (aggregates all specialist findings) --------------
            risk = await self._delegate(
                events, ctx, AgentRole.RISK,
                {"profile": profile.model_dump(), "kyc": findings.get("kyc", {}),
                 "aml": findings.get("aml", {}), "sanctions": findings.get("sanctions", {}),
                 "fraud": findings.get("fraud", {})},
                context_id, depth, failures, trace)
            findings["risk"] = risk
            sanctions = findings.get("sanctions", {})

            # --- HUMAN-IN-THE-LOOP gate ---------------------------------
            # High-stakes outcomes must not be auto-filed. Pause the task at
            # the A2A INPUT_REQUIRED state and wait for an analyst decision;
            # low-risk cases proceed straight to Reporting.
            if not failures and self.settings.require_human_review \
                    and _requires_review(risk, sanctions):
                await events.update_status(
                    ctx.task, TaskState.INPUT_REQUIRED,
                    f"⏸ Human review required — recommended: {risk.get('decision')} "
                    f"({risk.get('risk_band')}). Awaiting analyst approval before "
                    f"the report is filed.")
                await self._record(caller, "review_requested", profile.full_name,
                                   "input_required",
                                   {"decision": risk.get("decision"),
                                    "band": risk.get("risk_band")})
                return  # task stays INPUT_REQUIRED until a decision resumes it

            # --- Reporting (needs everything) ---------------------------
            await self._delegate(
                events, ctx, AgentRole.REPORTING,
                {"profile": profile.model_dump(), "kyc": findings.get("kyc", {}),
                 "aml": findings.get("aml", {}), "sanctions": sanctions,
                 "fraud": findings.get("fraud", {}), "risk": risk},
                context_id, depth, failures, trace)

            # A partial pipeline must NOT masquerade as a clean, low-risk result.
            if failures:
                await events.update_status(
                    ctx.task, TaskState.FAILED,
                    f"investigation degraded — no response from: {', '.join(failures)}")
                await self._record(caller, "investigation_failed", profile.full_name,
                                   "degraded", {"failed": failures})
            else:
                band = risk.get("risk_band", "LOW")
                await events.update_status(ctx.task, TaskState.COMPLETED,
                                           f"investigation complete — {band} risk")
                await self._record(caller, "investigation_completed", profile.full_name,
                                   band, {"risk_score": risk.get("risk_score"),
                                          "sar_recommended": risk.get("sar_recommended")})
        finally:
            trace.wall_ms = round((time.perf_counter() - wall0) * 1000, 2)
            if self.trace_store is not None:
                self.trace_store.put(trace)
            if self.metrics is not None:
                self.metrics.inc("investigations_total")
                self.metrics.inc("agent_calls_total", trace.agent_calls)
                self.metrics.inc("errors_total", trace.errors)
                self.metrics.observe("investigation_latency_ms", trace.wall_ms)
            log_event(_log, "investigation_finished", context_id=context_id,
                      wall_ms=trace.wall_ms, agent_calls=trace.agent_calls,
                      errors=trace.errors, est_tokens=trace.est_tokens,
                      llm_tokens=trace.llm_tokens, cost_usd=trace.cost_usd)

    # ---------------------------------------------------------------------
    async def _resume(self, ctx: RequestContext, events: EventQueue,
                      human_decision: dict) -> None:
        """Finalise a paused (INPUT_REQUIRED) investigation after an analyst decision.

        Actions: **approve** (confirm the recommendation), **override** (set a new
        band/decision), or **close** (dismiss as a false positive). All produce
        the final report and drive the task to COMPLETED — the human has the last
        word over the automated recommendation.
        """
        task = ctx.task
        caller = ctx.caller or "analyst"
        by_name = {a.name: a.first_data() for a in task.artifacts}
        profile = parse_profile(self._profile_from_history(task))
        risk = dict(by_name.get("risk_assessment", {}) or {})

        action = str(human_decision.get("action", "approve")).lower()
        note = str(human_decision.get("note", "")).strip()
        await self._record(caller, "human_decision", profile.full_name, action,
                           {"note": note, "override_band": human_decision.get("override_band")})

        # Apply the analyst's authority over the automated recommendation.
        if action == "override" and human_decision.get("override_band"):
            band = str(human_decision["override_band"]).upper()
            risk.update(risk_band=band, risk_score=risk.get("risk_score"),
                        decision=_DECISION_FOR.get(band, risk.get("decision")),
                        sar_recommended=band in {"HIGH", "CRITICAL"})
        elif action == "close":
            risk.update(risk_band="LOW", decision="NO ACTION — closed by analyst",
                        sar_recommended=False)

        human_decision = {**human_decision, "decided_by": caller,
                          "final_decision": risk.get("decision")}

        await events.update_status(task, TaskState.WORKING,
                                   f"resuming after analyst decision: {action}")
        # Append the reporting hop to the ORIGINAL trace so the final view keeps
        # the full 6-agent timeline from the paused phase.
        trace = (self.trace_store.get(task.context_id) if self.trace_store else None) \
            or InvestigationTrace(context_id=task.context_id, task_id=task.id,
                                  customer=profile.full_name)
        wall0 = time.perf_counter()
        await self._delegate(
            events, ctx, AgentRole.REPORTING,
            {"profile": profile.model_dump(), "kyc": by_name.get("kyc_findings", {}),
             "aml": by_name.get("aml_findings", {}),
             "sanctions": by_name.get("sanctions_findings", {}),
             "fraud": by_name.get("fraud_findings", {}), "risk": risk,
             "human_decision": human_decision},
            task.context_id, 0, [], trace)

        await events.update_status(
            task, TaskState.COMPLETED,
            f"finalised after analyst {action} — {risk.get('decision')}")
        await self._record(caller, "investigation_completed", profile.full_name,
                           risk.get("risk_band", "LOW"),
                           {"human_action": action, "decision": risk.get("decision")})
        trace.wall_ms += round((time.perf_counter() - wall0) * 1000, 2)
        if self.trace_store is not None:
            self.trace_store.put(trace)

    @staticmethod
    def _profile_from_history(task) -> dict:
        for msg in task.history:
            data = msg.first_data()
            if isinstance(data, dict) and "profile" in data:
                return data["profile"]
        return {}

    # ---------------------------------------------------------------------
    async def _delegate(self, events: EventQueue, ctx: RequestContext,
                        role: AgentRole, data: dict, context_id: str, depth: int,
                        failures: list[str], trace: InvestigationTrace) -> dict:
        """Run one A2A round trip to a specialist; re-emit its artifact + trace it.

        On failure, records the role in ``failures`` and returns {} so the
        pipeline keeps going (partial data is still surfaced) — but the caller
        will mark the whole investigation FAILED afterwards.
        """
        await events.update_status(ctx.task, TaskState.WORKING,
                                   f"delegating to {role.value} agent via A2A")
        with trace.span(f"a2a:{role.value}", kind="a2a_call", agent=role.value) as span:
            span.est_tokens = estimate_payload_tokens(data)  # request size (estimated)
            try:
                card = await self._discover(role)
                rpc_url = card.rpc_url or self.peers[role]
                message = Message(
                    parts=[Part.from_data(data)],
                    context_id=context_id,
                    metadata={"delegation_depth": depth + 1, "from": self.role},
                )
                task = await self.client.send_message(rpc_url, message)  # type: ignore[union-attr]
            except A2AError as exc:
                failures.append(role.value)
                span.status, span.error = "error", exc.message
                await events.update_status(
                    ctx.task, TaskState.WORKING,
                    f"{role.value} agent failed ({exc.message}); continuing degraded")
                return {}

            if task.status.state != TaskState.COMPLETED or not task.artifacts:
                failures.append(role.value)
                span.status, span.error = "error", "no result"
                await events.update_status(
                    ctx.task, TaskState.WORKING,
                    f"{role.value} agent returned no result; continuing degraded")
                return {}

            artifact = task.artifacts[-1]
            await events.emit_artifact(ctx.task, artifact)
            result = artifact.first_data() or {}
            self._account(span, artifact, result)
            return result

    @staticmethod
    def _account(span: Span, artifact, result: dict) -> None:
        """Fold token/cost accounting from a specialist's artifact into its span."""
        span.est_tokens += estimate_payload_tokens(result)
        # Real LLM usage is only present on the Reporting artifact when enabled.
        llm = (artifact.metadata or {}).get("llm") if artifact.metadata else None
        if llm:
            span.llm_tokens = int(llm.get("input_tokens", 0)) + int(llm.get("output_tokens", 0))
            span.cost_usd = cost_usd(llm.get("model", ""), llm.get("input_tokens", 0),
                                     llm.get("output_tokens", 0))

    async def _discover(self, role: AgentRole) -> AgentCard:
        if role not in self._cards:
            self._cards[role] = await self.client.discover(self.peers[role])  # type: ignore[union-attr]
        return self._cards[role]
