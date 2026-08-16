"""Task store — owns the Task lifecycle.

A2A defines the Task and its states; *how* you persist them is an
implementation choice. Phase 2 ships an in-memory store behind a small
interface so Phase 4 can drop in a SQLite-backed one without touching agents
or the server.

Legal state transitions (we enforce them so a bug can't move a task backwards):

    SUBMITTED ─▶ WORKING ─▶ COMPLETED
        │           │        FAILED
        │           │        CANCELED
        │           └─────▶ INPUT_REQUIRED / AUTH_REQUIRED ─▶ WORKING ...
        └─▶ REJECTED
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from app.a2a.types import Artifact, Message, Task, TaskState, TaskStatus

# Which states may follow a given state. Terminal states have no successors.
_ALLOWED: dict[TaskState, set[TaskState]] = {
    TaskState.SUBMITTED: {TaskState.WORKING, TaskState.REJECTED, TaskState.FAILED,
                          TaskState.CANCELED},
    TaskState.WORKING: {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED,
                        TaskState.INPUT_REQUIRED, TaskState.AUTH_REQUIRED},
    TaskState.INPUT_REQUIRED: {TaskState.WORKING, TaskState.CANCELED, TaskState.FAILED},
    TaskState.AUTH_REQUIRED: {TaskState.WORKING, TaskState.CANCELED, TaskState.FAILED},
}


class IllegalTransition(Exception):
    pass


def assert_transition(current: TaskState, new: TaskState) -> None:
    """Raise IllegalTransition unless ``current → new`` is a legal lifecycle edge.

    Shared by every TaskStore implementation so the rules can't drift between
    the in-memory store and the SQLite store.
    """
    if current.is_terminal:
        raise IllegalTransition(f"task is terminal ({current}); cannot change to {new}")
    if new != current and new not in _ALLOWED.get(current, set()):
        raise IllegalTransition(f"{current} → {new} is not a legal transition")


class TaskStore(Protocol):
    """The interface agents and the server depend on (not the concrete class)."""

    async def create(self, task: Task) -> Task: ...
    async def get(self, task_id: str) -> Task | None: ...
    async def set_status(self, task_id: str, status: TaskStatus) -> Task: ...
    async def add_artifact(self, task_id: str, artifact: Artifact) -> Task: ...
    async def append_history(self, task_id: str, message: Message) -> Task: ...
    async def list(self, *, context_id: str | None = None) -> list[Task]: ...


class InMemoryTaskStore:
    """Async-safe in-memory implementation. Good enough for the demo and tests."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = asyncio.Lock()

    async def create(self, task: Task) -> Task:
        async with self._lock:
            self._tasks[task.id] = task
            return task

    async def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def set_status(self, task_id: str, status: TaskStatus) -> Task:
        async with self._lock:
            task = self._require(task_id)
            assert_transition(task.status.state, status.state)
            task.status = status
            return task

    async def add_artifact(self, task_id: str, artifact: Artifact) -> Task:
        async with self._lock:
            task = self._require(task_id)
            task.artifacts.append(artifact)
            return task

    async def append_history(self, task_id: str, message: Message) -> Task:
        async with self._lock:
            task = self._require(task_id)
            task.history.append(message)
            return task

    async def list(self, *, context_id: str | None = None) -> list[Task]:
        tasks = list(self._tasks.values())
        if context_id is not None:
            tasks = [t for t in tasks if t.context_id == context_id]
        return tasks

    def _require(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task
