"""Phase 4 — persistence, task lifecycle across a store, and restart replay.

Proves the SQLite store is a drop-in for the in-memory one, honours the same
lifecycle rules, keeps agents isolated by role, and that a completed
investigation survives a process "restart".
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.a2a.client import A2AClient
from app.a2a.task_store import IllegalTransition
from app.a2a.types import Artifact, Message, Part, Task, TaskState, TaskStatus
from app.agents.schemas import CustomerProfile
from app.api.factory import build_app
from app.config import AgentRole, Settings
from app.database.repository import InvestigationRepository
from app.database.session import init_models, make_engine, make_session_factory
from app.database.task_store import SqlTaskStore


def _db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db"


# --------------------------------------------------------------------------- #
# SqlTaskStore unit behaviour
# --------------------------------------------------------------------------- #
async def test_sql_store_crud_and_scoping(tmp_path):
    engine = make_engine(_db_url(tmp_path))
    await init_models(engine)
    sf = make_session_factory(engine)
    lock = asyncio.Lock()

    kyc = SqlTaskStore("kyc", sf, lock)
    aml = SqlTaskStore("aml", sf, lock)

    task = await kyc.create(Task())
    await kyc.set_status(task.id, TaskStatus(state=TaskState.WORKING))
    await kyc.add_artifact(task.id, Artifact.of_text("kyc_findings", "ok"))
    await kyc.set_status(task.id, TaskStatus(state=TaskState.COMPLETED))

    loaded = await kyc.get(task.id)
    assert loaded is not None
    assert loaded.status.state == TaskState.COMPLETED
    assert loaded.artifacts[0].parts[0].text == "ok"

    # Per-role scoping: the AML store cannot see a KYC task.
    assert await aml.get(task.id) is None

    # Terminal transitions are rejected here too (shared validator).
    with pytest.raises(IllegalTransition):
        await kyc.set_status(task.id, TaskStatus(state=TaskState.WORKING))

    await engine.dispose()


# --------------------------------------------------------------------------- #
# Full investigation persisted + restart replay
# --------------------------------------------------------------------------- #
async def test_investigation_persists_across_restart(tmp_path):
    settings = Settings(require_human_review=False)
    url = _db_url(tmp_path)

    # --- first "process": run an investigation --------------------------
    app1 = build_app(settings, database_url=url)
    await init_models(app1.state.db_engine)
    transport = httpx.ASGITransport(app=app1)
    app1.state.orchestrator.set_client(A2AClient(transport=transport, max_attempts=1))
    user = A2AClient(transport=transport, max_attempts=1)

    msg = Message(parts=[Part.from_data({"profile": CustomerProfile.demo().model_dump()})])
    task = await user.send_message(settings.orchestrator_url, msg)
    assert task.status.state == TaskState.COMPLETED

    # It's listable through the repository.
    investigations = await app1.state.investigations.list()
    assert len(investigations) == 1
    assert investigations[0].customer == "Viktor Petrov"
    assert investigations[0].risk_band == "CRITICAL"
    assert investigations[0].sar_recommended is True

    await user.aclose()
    await app1.state.orchestrator.aclose()
    await app1.state.db_engine.dispose()

    # --- second "process": a fresh app on the same DB file --------------
    engine2 = make_engine(url)
    repo2 = InvestigationRepository(make_session_factory(engine2))
    replayed = await repo2.list()
    assert len(replayed) == 1                       # survived the "restart"
    full = await repo2.get(replayed[0].task_id)
    assert full is not None
    names = [a.name for a in full.artifacts]
    assert names == ["kyc_findings", "aml_findings", "sanctions_findings",
                     "fraud_findings", "risk_assessment", "investigation_report"]
    await engine2.dispose()


async def test_persistent_agents_isolate_tasks_by_role(tmp_path):
    """Each agent's persistent store only sees its own role's tasks."""
    settings = Settings(require_human_review=False)
    app = build_app(settings, database_url=_db_url(tmp_path))
    await init_models(app.state.db_engine)
    transport = httpx.ASGITransport(app=app)
    app.state.orchestrator.set_client(A2AClient(transport=transport, max_attempts=1))
    user = A2AClient(transport=transport, max_attempts=1)

    msg = Message(parts=[Part.from_data({"profile": CustomerProfile.demo().model_dump()})])
    task = await user.send_message(settings.orchestrator_url, msg)

    agents = app.state.agents
    kyc_tasks = await agents[AgentRole.KYC].tasks.list(context_id=task.context_id)
    orch_tasks = await agents[AgentRole.ORCHESTRATOR].tasks.list(context_id=task.context_id)
    assert len(kyc_tasks) == 1                       # exactly its own task
    assert len(orch_tasks) == 1
    assert kyc_tasks[0].id != orch_tasks[0].id

    await user.aclose()
    await app.state.orchestrator.aclose()
    await app.state.db_engine.dispose()
