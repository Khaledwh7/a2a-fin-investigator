"""Persistence, task lifecycle across a store, and restart replay.

Proves the SQLite store is a drop-in for the in-memory one, honours the same
lifecycle rules, keeps agents isolated by role, and that a completed
investigation survives a process "restart".
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.a2a.task_store import IllegalTransition
from app.a2a.types import Artifact, Message, Part, Task, TaskState, TaskStatus
from app.agents.schemas import CustomerProfile
from app.api.factory import build_app
from app.config import AgentRole, Settings
from app.database.repository import InvestigationRepository
from app.database.session import init_models, make_engine, make_session_factory
from app.database.task_store import SqlTaskStore
from tests.conftest import user_client, wire_orchestrator


def _db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db"


# --------------------------------------------------------------------------- #
# Deletion — a case is removed as a unit, and the removal is itself recorded
# --------------------------------------------------------------------------- #
async def test_deleting_an_investigation_removes_every_agent_task(tmp_path):
    """The specialists' tasks share the orchestrator's contextId; deleting the
    case must not leave them orphaned in the store."""
    settings = Settings(require_human_review=False)
    app = build_app(settings, database_url=_db_url(tmp_path))
    transport = httpx.ASGITransport(app=app)
    wire_orchestrator(app, transport)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=transport, base_url="http://api") as http:
        created = (await http.post(
            "/investigations",
            json={"profile": CustomerProfile.demo().model_dump()})).json()
        task_id = created["task"]["id"]
        context_id = created["task"]["contextId"]

        # Every role holds a task under the shared context before deletion.
        before = {role: len(await agent.tasks.list(context_id=context_id))
                  for role, agent in app.state.agents.items()}
        assert sum(before.values()) == 7          # orchestrator + six specialists

        deleted = (await http.request("DELETE", f"/investigations/{task_id}")).json()
        assert deleted["deleted"] == 7
        assert deleted["customer"] == "Viktor Petrov"

        after = {role: len(await agent.tasks.list(context_id=context_id))
                 for role, agent in app.state.agents.items()}
        assert sum(after.values()) == 0, after
        assert (await http.get(f"/investigations/{task_id}")).status_code == 404
        assert (await http.request("DELETE",
                                   f"/investigations/{task_id}")).status_code == 404

        # Removing the case must not remove the evidence it existed.
        actions = [e["action"] for e in (await http.get("/audit")).json()["entries"]]
        assert "investigation_deleted" in actions


async def test_clearing_the_audit_log_starts_a_new_verifiable_chain(tmp_path):
    """Entries are not individually deletable; clearing opens a fresh chain
    that documents the clearance rather than forging an unbroken history."""
    settings = Settings(require_human_review=False)
    app = build_app(settings, database_url=_db_url(tmp_path))
    transport = httpx.ASGITransport(app=app)
    wire_orchestrator(app, transport)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=transport, base_url="http://api") as http:
        await http.post("/investigations",
                        json={"profile": CustomerProfile.demo().model_dump()})
        before = (await http.get("/audit")).json()
        assert before["count"] > 1 and before["chain_valid"] is True

        cleared = (await http.request("DELETE", "/audit")).json()
        assert cleared["cleared"] == before["count"]

        after = (await http.get("/audit")).json()
        assert after["count"] == 1                     # only the clearance itself
        assert after["chain_valid"] is True            # a real chain, from genesis
        entry = after["entries"][0]
        assert entry["action"] == "audit_log_cleared"
        assert entry["detail"]["entries_removed"] == before["count"]


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
    wire_orchestrator(app1, transport)
    user = user_client(app1, transport)

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
    wire_orchestrator(app, transport)
    user = user_client(app, transport)

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
