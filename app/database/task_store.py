"""SQLite-backed TaskStore — the persistent implementation of the Phase 2 interface.

It satisfies the exact same ``TaskStore`` protocol as ``InMemoryTaskStore``, so
agents and the server don't change at all — persistence is a drop-in.

Two things worth noting for an interviewer:

  * **It returns copies, not shared objects.** ``get()`` deserializes a fresh
    Task from JSON. That's why the Phase 3 "single-writer" fix matters: if the
    emitter had kept mutating the caller's Task, those mutations would silently
    vanish here. The server's persistence loop is the one writer.
  * **Per-agent scoping.** Each store is bound to one ``agent_role`` and only
    ever sees that role's tasks — the same isolation you'd get if every agent
    had its own database in a multi-container deployment.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.a2a.task_store import assert_transition
from app.a2a.types import Artifact, Message, Task, TaskStatus
from app.database.models import TaskRecord


class SqlTaskStore:
    def __init__(self, agent_role: str, session_factory: async_sessionmaker[AsyncSession],
                 write_lock: asyncio.Lock) -> None:
        self.role = agent_role
        self._sf = session_factory
        # A single shared write-lock serializes writes across all agent stores.
        # SQLite allows only one writer at a time; serializing avoids
        # "database is locked" under the concurrent drain loops of one run.
        self._lock = write_lock

    async def create(self, task: Task) -> Task:
        async with self._lock, self._sf() as s:
            s.add(TaskRecord(
                id=task.id, context_id=task.context_id, agent_role=self.role,
                state=task.status.state.value, task_json=task.to_wire()))
            await s.commit()
        return task

    async def get(self, task_id: str) -> Task | None:
        async with self._sf() as s:
            rec = await s.get(TaskRecord, task_id)
        if rec is None or rec.agent_role != self.role:
            return None
        return Task.model_validate(rec.task_json)

    async def set_status(self, task_id: str, status: TaskStatus) -> Task:
        async with self._lock, self._sf() as s:
            rec = await self._require(s, task_id)
            task = Task.model_validate(rec.task_json)
            assert_transition(task.status.state, status.state)
            task.status = status
            self._flush(rec, task)
            await s.commit()
            return task

    async def add_artifact(self, task_id: str, artifact: Artifact) -> Task:
        async with self._lock, self._sf() as s:
            rec = await self._require(s, task_id)
            task = Task.model_validate(rec.task_json)
            task.artifacts.append(artifact)
            self._flush(rec, task)
            await s.commit()
            return task

    async def append_history(self, task_id: str, message: Message) -> Task:
        async with self._lock, self._sf() as s:
            rec = await self._require(s, task_id)
            task = Task.model_validate(rec.task_json)
            task.history.append(message)
            self._flush(rec, task)
            await s.commit()
            return task

    async def list(self, *, context_id: str | None = None) -> list[Task]:
        stmt = select(TaskRecord).where(TaskRecord.agent_role == self.role)
        if context_id is not None:
            stmt = stmt.where(TaskRecord.context_id == context_id)
        stmt = stmt.order_by(TaskRecord.created_at)
        async with self._sf() as s:
            rows = (await s.execute(stmt)).scalars().all()
        return [Task.model_validate(r.task_json) for r in rows]

    # -- helpers ----------------------------------------------------------
    async def _require(self, s: AsyncSession, task_id: str) -> TaskRecord:
        rec = await s.get(TaskRecord, task_id)
        if rec is None or rec.agent_role != self.role:
            raise KeyError(task_id)
        return rec

    @staticmethod
    def _flush(rec: TaskRecord, task: Task) -> None:
        rec.task_json = task.to_wire()
        rec.state = task.status.state.value
