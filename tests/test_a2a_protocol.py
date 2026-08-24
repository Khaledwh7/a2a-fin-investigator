"""Protocol conformance and end-to-end plumbing.

These tests pin the two things most tutorials get wrong about A2A v1.0:
the camelCase wire format and the SCREAMING_SNAKE enum values. They also drive
a full client → server → executor round trip in-process (no live socket) using
an EchoExecutor, proving discovery, SendMessage, GetTask and streaming work.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.a2a.agent_card import build_agent_card
from app.a2a.client import A2AClient
from app.a2a.executor import EchoExecutor
from app.a2a.server import A2AAgent, build_router
from app.a2a.task_store import IllegalTransition, InMemoryTaskStore
from app.a2a.types import (
    AgentSkill,
    Message,
    Part,
    Task,
    TaskState,
    TaskStatus,
    canonical_method,
)


# --------------------------------------------------------------------------- #
# Wire-format conformance
# --------------------------------------------------------------------------- #
def test_message_wire_is_camelcase_with_snake_enums():
    msg = Message.user_text("hello", context_id="ctx1")
    msg.task_id = "task1"
    wire = msg.to_wire()

    assert wire["messageId"].startswith("msg_")
    assert wire["contextId"] == "ctx1"
    assert wire["taskId"] == "task1"
    assert wire["role"] == "ROLE_USER"          # SCREAMING_SNAKE, not "user"
    assert wire["parts"][0]["text"] == "hello"


def test_task_state_values_and_terminality():
    assert TaskState.WORKING.value == "TASK_STATE_WORKING"
    assert TaskState.COMPLETED.is_terminal
    assert not TaskState.WORKING.is_terminal


def test_part_requires_exactly_one_content():
    with pytest.raises(ValueError):
        Part()                       # nothing set
    with pytest.raises(ValueError):
        Part(text="a", url="b")      # two set
    assert Part.from_text("ok").kind == "text"
    assert Part.from_data({"x": 1}).kind == "data"
    assert Part.from_uri("http://f", filename="f.pdf").kind == "file"


def test_legacy_method_aliases_map_to_v1():
    assert canonical_method("message/send") == "SendMessage"
    assert canonical_method("tasks/get") == "GetTask"
    assert canonical_method("SendMessage") == "SendMessage"  # already canonical


# --------------------------------------------------------------------------- #
# Task store lifecycle
# --------------------------------------------------------------------------- #
async def test_task_store_enforces_legal_transitions():
    store = InMemoryTaskStore()
    task = await store.create(Task())
    await store.set_status(task.id, TaskStatus(state=TaskState.WORKING))
    await store.set_status(task.id, TaskStatus(state=TaskState.COMPLETED))
    # COMPLETED is terminal — any further change must be rejected.
    with pytest.raises(IllegalTransition):
        await store.set_status(task.id, TaskStatus(state=TaskState.WORKING))


async def test_task_store_illegal_skip_rejected():
    store = InMemoryTaskStore()
    task = await store.create(Task())
    with pytest.raises(IllegalTransition):
        # SUBMITTED → COMPLETED is not a legal edge.
        await store.set_status(task.id, TaskStatus(state=TaskState.COMPLETED))


# --------------------------------------------------------------------------- #
# End-to-end: client → server → executor  (in-process ASGI transport)
# --------------------------------------------------------------------------- #
def _make_app() -> FastAPI:
    card = build_agent_card(
        name="Echo Agent",
        description="repeats input; used for protocol tests",
        rpc_url="http://echo/a2a/echo",
        skills=[AgentSkill(id="echo", name="Echo", description="echo text",
                           tags=["test"])],
    )
    agent = A2AAgent(card, EchoExecutor(), InMemoryTaskStore())
    app = FastAPI()
    app.include_router(build_router(agent), prefix="/a2a/echo")
    return app


@pytest.fixture
def client() -> A2AClient:
    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    return A2AClient(transport=transport, max_attempts=1)


async def test_discovery_returns_agent_card(client: A2AClient):
    card = await client.discover("http://echo/a2a/echo")
    assert card.name == "Echo Agent"
    assert card.rpc_url == "http://echo/a2a/echo"
    assert card.capabilities.streaming is True
    await client.aclose()


async def test_send_message_runs_to_completion_with_artifact(client: A2AClient):
    task = await client.send_message(
        "http://echo/a2a/echo", Message.user_text("investigate me"))
    assert task.status.state == TaskState.COMPLETED
    assert task.artifacts, "executor should have produced an artifact"
    assert task.artifacts[0].parts[0].text == "investigate me"

    # GetTask returns the same terminal task.
    fetched = await client.get_task("http://echo/a2a/echo", task.id)
    assert fetched.id == task.id
    assert fetched.status.state == TaskState.COMPLETED
    await client.aclose()


async def test_streaming_emits_status_and_artifact_frames(client: A2AClient):
    kinds: list[str] = []
    async for frame in client.stream_message(
        "http://echo/a2a/echo", Message.user_text("stream me")):
        kinds.append(next(iter(frame.keys())))
    # First frame is the Task, then status/artifact updates.
    assert kinds[0] == "task"
    assert "statusUpdate" in kinds
    assert "artifactUpdate" in kinds
    await client.aclose()


async def test_context_id_threads_through(client: A2AClient):
    msg = Message.user_text("hi", context_id="case-42")
    task = await client.send_message("http://echo/a2a/echo", msg)
    assert task.context_id == "case-42"
    await client.aclose()
