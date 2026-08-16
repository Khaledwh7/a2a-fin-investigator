"""SQLAlchemy models.

We persist one row per A2A Task. The full Task (status, history of Messages,
Artifacts) is stored as JSON in ``task_json`` — the Task is already a Pydantic
model with a spec-exact wire form, so JSON is the faithful, low-friction
representation. The scalar columns (``context_id``, ``agent_role``, ``state``,
timestamps) are lifted out for indexing, filtering and sorting.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TaskRecord(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    context_id: Mapped[str] = mapped_column(String, index=True)
    agent_role: Mapped[str] = mapped_column(String, index=True)
    state: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    task_json: Mapped[dict] = mapped_column(JSON)


class AuditRecord(Base):
    """Append-only, hash-chained audit log (tamper-evident).

    Each row's ``entry_hash`` = sha256(prev_hash + canonical(row)). Changing any
    historical row breaks the chain from that point on, which ``verify_chain``
    detects. Append-only: we never UPDATE or DELETE these rows.
    """

    __tablename__ = "audit_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String, index=True)
    resource: Mapped[str] = mapped_column(String)
    outcome: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String)
    entry_hash: Mapped[str] = mapped_column(String)
