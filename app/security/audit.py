"""Audit logging — a tamper-evident record of security-relevant events.

Every entry is hash-chained to the previous one:

    entry_hash = sha256(prev_hash + canonical(actor, action, resource, outcome, detail, ts))

So the log is *append-only* and *tamper-evident*: silently editing or deleting a
past entry breaks every hash after it, which ``verify_chain`` detects. We record
auth allow/deny, investigation lifecycle, prompt-injection detections and the
like.

Two implementations behind one Protocol: an in-memory one (tests / no DB) and a
SQLite-backed one (the running app). Same single write-lock as the task store so
SQLite never sees concurrent writers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import AuditRecord

_GENESIS = "0" * 64


def _hash(prev_hash: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


@dataclass
class AuditEntry:
    seq: int
    ts: str
    actor: str
    action: str
    resource: str
    outcome: str
    detail: dict[str, Any]
    prev_hash: str
    entry_hash: str

    def _payload(self) -> dict[str, Any]:
        return {"ts": self.ts, "actor": self.actor, "action": self.action,
                "resource": self.resource, "outcome": self.outcome, "detail": self.detail}


class AuditLogger(Protocol):
    async def record(self, *, actor: str, action: str, resource: str,
                     outcome: str, detail: dict[str, Any] | None = None) -> None: ...
    async def entries(self) -> list[AuditEntry]: ...
    async def verify_chain(self) -> bool: ...


class MemoryAuditLogger:
    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []
        self._lock = asyncio.Lock()

    async def record(self, *, actor: str, action: str, resource: str,
                     outcome: str, detail: dict[str, Any] | None = None) -> None:
        async with self._lock:
            prev = self._entries[-1].entry_hash if self._entries else _GENESIS
            payload = {"ts": datetime.now(UTC).isoformat(), "actor": actor,
                       "action": action, "resource": resource, "outcome": outcome,
                       "detail": detail or {}}
            entry = AuditEntry(seq=len(self._entries) + 1, prev_hash=prev,
                               entry_hash=_hash(prev, payload), **payload)
            self._entries.append(entry)

    async def entries(self) -> list[AuditEntry]:
        return list(self._entries)

    async def verify_chain(self) -> bool:
        return _verify(self._entries)


class DbAuditLogger:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession],
                 write_lock: asyncio.Lock) -> None:
        self._sf = session_factory
        self._lock = write_lock

    async def record(self, *, actor: str, action: str, resource: str,
                     outcome: str, detail: dict[str, Any] | None = None) -> None:
        async with self._lock, self._sf() as s:
            last = (await s.execute(
                select(AuditRecord).order_by(AuditRecord.seq.desc()).limit(1))
            ).scalar_one_or_none()
            prev = last.entry_hash if last else _GENESIS
            payload = {"ts": datetime.now(UTC).isoformat(), "actor": actor,
                       "action": action, "resource": resource, "outcome": outcome,
                       "detail": detail or {}}
            s.add(AuditRecord(prev_hash=prev, entry_hash=_hash(prev, payload), **payload))
            await s.commit()

    async def entries(self) -> list[AuditEntry]:
        async with self._sf() as s:
            rows = (await s.execute(
                select(AuditRecord).order_by(AuditRecord.seq))).scalars().all()
        return [AuditEntry(seq=r.seq, ts=r.ts, actor=r.actor, action=r.action,
                           resource=r.resource, outcome=r.outcome, detail=r.detail,
                           prev_hash=r.prev_hash, entry_hash=r.entry_hash) for r in rows]

    async def verify_chain(self) -> bool:
        return _verify(await self.entries())


def _verify(entries: list[AuditEntry]) -> bool:
    prev = _GENESIS
    for e in entries:
        if e.prev_hash != prev or e.entry_hash != _hash(prev, e._payload()):
            return False
        prev = e.entry_hash
    return True
