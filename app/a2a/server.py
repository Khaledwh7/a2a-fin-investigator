"""A2A server — JSON-RPC 2.0 dispatch + SSE streaming.

An ``A2AAgent`` bundles one role's Agent Card, its executor and a task store.
``build_router()`` turns that bundle into a FastAPI router exposing the two
endpoints every A2A server needs:

    GET  {base}/.well-known/agent-card.json   → the Agent Card (discovery)
    POST {base}                               → JSON-RPC (SendMessage, GetTask, …)

The POST handler implements the v1.0 method set (plus v0.3 aliases) and the
`A2A-Version` header negotiation. Streaming methods return `text/event-stream`;
everything else returns a JSON-RPC response object.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.a2a.errors import (
    A2AError,
    JSONRPCErrorCode,
    TransportAuthError,
    internal_error,
    invalid_params,
    method_not_found,
    task_not_cancelable,
    task_not_found,
    unsupported_operation,
    version_not_supported,
)
from app.a2a.executor import AgentExecutor, EventQueue, RequestContext
from app.a2a.task_store import InMemoryTaskStore, TaskStore
from app.a2a.types import (
    AgentCard,
    Message,
    Method,
    Task,
    TaskState,
    TaskStatus,
    canonical_method,
)

# Identity that authenticated a request (the caller's ``sub``), or None when auth
# is disabled. The factory injects a real JWT authenticator in Phase 5; the
# server itself imports nothing from app/security.
Authenticator = Callable[[dict[str, str]], Awaitable["str | None"]]
# Validates an inbound Message (parts/size limits); injected, may be None.
MessageValidator = Callable[["Message"], None]


async def _no_auth(_headers: dict[str, str]) -> str | None:
    return None


class A2AAgent:
    """One agent's server-side bundle: card + executor + task store."""

    def __init__(
        self,
        card: AgentCard,
        executor: AgentExecutor,
        task_store: TaskStore | None = None,
        *,
        supported_version: str = "1.0",
        accept_legacy_v03: bool = True,
        authenticator: Authenticator = _no_auth,
        message_validator: MessageValidator | None = None,
        task_timeout_seconds: float = 60.0,
    ) -> None:
        self.card = card
        self.executor = executor
        self.tasks = task_store or InMemoryTaskStore()
        self.supported_version = supported_version
        self.accept_legacy_v03 = accept_legacy_v03
        self.authenticate = authenticator
        self.message_validator = message_validator
        self.task_timeout = task_timeout_seconds

    # -- version negotiation (spec §3.6.1) --------------------------------
    def _check_version(self, header_value: str | None) -> None:
        if not header_value:
            return  # missing header → assume current, per spec
        major_minor = header_value.strip()
        if major_minor == self.supported_version:
            return
        if major_minor == "0.3" and self.accept_legacy_v03:
            return
        raise version_not_supported(major_minor, self.supported_version)

    # -- build the Task for an incoming message ---------------------------
    async def _prepare_task(self, message: Message) -> Task:
        if message.task_id:
            existing = await self.tasks.get(message.task_id)
            if existing:
                await self.tasks.append_history(existing.id, message)
                return existing
        task = Task(context_id=message.context_id or Task().context_id)
        task.history.append(message)
        message.context_id = task.context_id
        message.task_id = task.id
        await self.tasks.create(task)
        await self.tasks.set_status(task.id, TaskStatus(state=TaskState.SUBMITTED))
        return task

    # -- run the executor, draining events into the task store ------------
    async def _execute(self, ctx: RequestContext, events: EventQueue) -> None:
        """Run the executor and persist every event it publishes.

        The caller applies the timeout (via ``asyncio.wait_for``); if it fires,
        this coroutine is cancelled and we cancel the runner with it so no work
        is left dangling.
        """
        async def _runner() -> None:
            try:
                await self.executor.run(ctx, events)
            except Exception as exc:  # noqa: BLE001 — convert any failure to FAILED
                await events.update_status(ctx.task, TaskState.FAILED, f"error: {exc}")
            finally:
                await events.close()

        run = asyncio.create_task(_runner())
        try:
            async for event in events.events():
                await self._persist(event)
        finally:
            if not run.done():
                run.cancel()
            with contextlib.suppress(BaseException):
                await run

    async def _persist(self, event: Any) -> None:
        from app.a2a.types import TaskArtifactUpdateEvent, TaskStatusUpdateEvent
        if isinstance(event, TaskStatusUpdateEvent):
            try:
                await self.tasks.set_status(event.task_id, event.status)
            except Exception:  # noqa: BLE001 — an illegal transition shouldn't kill the stream
                pass
        elif isinstance(event, TaskArtifactUpdateEvent):
            await self.tasks.add_artifact(event.task_id, event.artifact)

    # -- non-streaming SendMessage ----------------------------------------
    async def send_message(self, params: dict, caller: str | None) -> dict:
        message = _parse_message(params)
        if self.message_validator is not None:
            self.message_validator(message)
        task = await self._prepare_task(message)
        ctx = RequestContext(message=message, task=task, caller=caller)
        events = EventQueue()
        try:
            await asyncio.wait_for(self._execute(ctx, events), timeout=self.task_timeout)
        except TimeoutError:
            await self.tasks.set_status(
                task.id, TaskStatus(state=TaskState.FAILED,
                                    message=Message.agent_text("task timed out")))
        final = await self.tasks.get(task.id) or task
        # SendMessageResponse is a oneof {task, message}; we always return a task.
        return final.to_wire()

    # -- streaming SendStreamingMessage -----------------------------------
    async def stream_message(self, params: dict, caller: str | None
                             ) -> AsyncIterator[dict]:
        message = _parse_message(params)
        if self.message_validator is not None:
            self.message_validator(message)
        task = await self._prepare_task(message)
        ctx = RequestContext(message=message, task=task, caller=caller)
        events = EventQueue()

        # First SSE frame is the initial Task (spec: StreamResponse.task).
        yield {"task": task.to_wire()}

        async def _runner() -> None:
            try:
                await self.executor.run(ctx, events)
            except Exception as exc:  # noqa: BLE001
                await events.update_status(ctx.task, TaskState.FAILED, f"error: {exc}")
            finally:
                await events.close()

        run = asyncio.create_task(_runner())
        from app.a2a.types import TaskArtifactUpdateEvent, TaskStatusUpdateEvent
        try:
            async for event in events.events():
                await self._persist(event)
                if isinstance(event, TaskStatusUpdateEvent):
                    yield {"statusUpdate": event.to_wire()}
                elif isinstance(event, TaskArtifactUpdateEvent):
                    yield {"artifactUpdate": event.to_wire()}
            await run
        finally:
            if not run.done():
                run.cancel()

    # -- GetTask ----------------------------------------------------------
    async def get_task(self, params: dict) -> dict:
        task_id = params.get("id") or params.get("taskId")
        if not task_id:
            raise invalid_params("GetTask requires 'id'")
        task = await self.tasks.get(task_id)
        if task is None:
            raise task_not_found(task_id)
        return task.to_wire()

    # -- CancelTask -------------------------------------------------------
    async def cancel_task(self, params: dict) -> dict:
        task_id = params.get("id") or params.get("taskId")
        if not task_id:
            raise invalid_params("CancelTask requires 'id'")
        task = await self.tasks.get(task_id)
        if task is None:
            raise task_not_found(task_id)
        if task.status.state.is_terminal:
            raise task_not_cancelable(task_id)
        updated = await self.tasks.set_status(
            task_id, TaskStatus(state=TaskState.CANCELED,
                                message=Message.agent_text("canceled by request")))
        return updated.to_wire()


def _parse_message(params: dict) -> Message:
    raw = params.get("message")
    if raw is None:
        raise invalid_params("params.message is required")
    try:
        return Message.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        raise invalid_params(f"invalid message: {exc}") from exc


# ---------------------------------------------------------------------------
# FastAPI wiring
# ---------------------------------------------------------------------------
def _rpc_ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_err(req_id: Any, err: A2AError) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": err.to_jsonrpc()}


def _sse(payload: dict, req_id: Any) -> str:
    """Format one JSON-RPC result as an SSE `data:` frame."""
    frame = {"jsonrpc": "2.0", "id": req_id, "result": payload}
    return f"data: {json.dumps(frame)}\n\n"


def build_router(agent: A2AAgent) -> APIRouter:
    """Return a FastAPI router for one agent (mount it under the agent's base path)."""
    router = APIRouter()

    @router.get("/.well-known/agent-card.json")
    async def agent_card() -> JSONResponse:  # noqa: ANN202
        return JSONResponse(agent.card.to_wire())

    @router.post("")
    async def rpc(request: Request,
                  a2a_version: str | None = Header(default=None, alias="A2A-Version")):
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                _rpc_err(None, A2AError(JSONRPCErrorCode.PARSE_ERROR, "invalid JSON")),
                status_code=200)

        req_id = body.get("id")
        try:
            agent._check_version(a2a_version)
            caller = await agent.authenticate(headers)   # authn + authz (injected)
            method = canonical_method(body.get("method", ""))
            params = body.get("params") or {}
            if not isinstance(params, dict):
                raise invalid_params("params must be an object")

            if method == Method.SEND_MESSAGE:
                return JSONResponse(_rpc_ok(req_id, await agent.send_message(params, caller)))

            if method == Method.SEND_STREAMING_MESSAGE:
                async def gen() -> AsyncIterator[str]:
                    async for frame in agent.stream_message(params, caller):
                        yield _sse(frame, req_id)
                return StreamingResponse(gen(), media_type="text/event-stream")

            if method == Method.GET_TASK:
                return JSONResponse(_rpc_ok(req_id, await agent.get_task(params)))

            if method == Method.CANCEL_TASK:
                return JSONResponse(_rpc_ok(req_id, await agent.cancel_task(params)))

            if method in (Method.SUBSCRIBE_TO_TASK, Method.LIST_TASKS):
                raise unsupported_operation(method)

            raise method_not_found(body.get("method", ""))

        except TransportAuthError as exc:
            # Auth is transport-level in A2A → real HTTP status, not JSON-RPC.
            headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
            return JSONResponse({"error": exc.message}, status_code=exc.status_code,
                                headers=headers)
        except A2AError as err:
            return JSONResponse(_rpc_err(req_id, err), status_code=200)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(_rpc_err(req_id, internal_error(str(exc))), status_code=200)

    return router
