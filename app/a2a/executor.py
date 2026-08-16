"""AgentExecutor — the contract every agent implements.

This is the seam between the protocol (app/a2a) and the domain (app/agents).
The server hands an executor a `RequestContext` (the incoming message + task)
and an `EventQueue`; the executor does its work and *publishes events* rather
than returning a value. That inversion is what makes streaming and non-streaming
use the exact same agent code:

  - non-streaming SendMessage  → server drains the queue, returns the final Task
  - streaming SendStreamingMessage → server forwards each event as it arrives (SSE)

The pattern mirrors the official a2a SDK (execute/cancel + an event queue), so
the shape is familiar to anyone who has read it.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from app.a2a.types import (
    Artifact,
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)

# Anything an executor can publish onto the queue.
Event = Task | Message | TaskStatusUpdateEvent | TaskArtifactUpdateEvent


class RequestContext:
    """Everything an executor needs about the call it is serving."""

    def __init__(self, message: Message, task: Task,
                 caller: str | None = None, metadata: dict[str, Any] | None = None):
        self.message = message      # the incoming Message
        self.task = task            # the Task created for this request
        self.caller = caller        # authenticated identity of the caller (Phase 5)
        self.metadata = metadata or {}

    @property
    def context_id(self) -> str:
        return self.task.context_id

    @property
    def task_id(self) -> str:
        return self.task.id


class EventQueue:
    """An async channel the executor writes events to and the server reads from.

    ``close()`` signals "no more events"; the server's drain loop stops there.
    """

    _SENTINEL = object()

    def __init__(self) -> None:
        self._q: asyncio.Queue[Any] = asyncio.Queue()

    async def publish(self, event: Event) -> None:
        await self._q.put(event)

    async def close(self) -> None:
        await self._q.put(self._SENTINEL)

    async def events(self):
        """Async iterator over published events, ending at close()."""
        while True:
            item = await self._q.get()
            if item is self._SENTINEL:
                return
            yield item

    # --- convenience emitters (agents call these) ----------------------
    # NOTE: these ONLY publish events. The server's persistence loop is the
    # single writer to the task store — so the emitters never mutate `task`
    # directly. That keeps the design correct for a copy-returning store
    # (e.g. the SQLite store in Phase 4), where mutating the local object
    # would otherwise be lost or, with a shared object, double-counted.
    async def update_status(self, task: Task, state: TaskState,
                            note: str | None = None) -> None:
        status = TaskStatus(
            state=state,
            message=Message.agent_text(note, context_id=task.context_id) if note else None,
        )
        await self.publish(TaskStatusUpdateEvent(
            task_id=task.id, context_id=task.context_id, status=status))

    async def emit_artifact(self, task: Task, artifact: Artifact) -> None:
        await self.publish(TaskArtifactUpdateEvent(
            task_id=task.id, context_id=task.context_id, artifact=artifact))


class AgentExecutor(ABC):
    """Base class for all six agents. Subclasses implement `run`."""

    #: overridden by each agent; used for tracing/authz
    role: str = "agent"

    @abstractmethod
    async def run(self, ctx: RequestContext, events: EventQueue) -> None:
        """Do the work. Publish status updates and artifacts onto `events`.

        Must drive the task to a terminal state (COMPLETED / FAILED / ...).
        The server wraps this with a timeout and turns exceptions into FAILED,
        so implementations can focus on the happy path.
        """
        raise NotImplementedError

    async def cancel(self, ctx: RequestContext, events: EventQueue) -> None:
        """Optional: react to a CancelTask. Default just marks canceled."""
        await events.update_status(ctx.task, TaskState.CANCELED, "canceled by request")


class EchoExecutor(AgentExecutor):
    """A trivial executor used by the Phase 2 tests to prove the plumbing works.

    Real domain agents arrive in Phase 3.
    """

    role = "echo"

    async def run(self, ctx: RequestContext, events: EventQueue) -> None:
        await events.update_status(ctx.task, TaskState.WORKING, "echoing")
        artifact = Artifact.of_text("echo", ctx.message.text or "(no text)",
                                    description="verbatim echo of the input")
        await events.emit_artifact(ctx.task, artifact)
        await events.update_status(ctx.task, TaskState.COMPLETED, "done")
