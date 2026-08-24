"""A2A client — how one agent talks to another.

Responsibilities:
  * **Discovery** — fetch a peer's Agent Card from its well-known URI.
  * **Communication** — JSON-RPC SendMessage / SendStreamingMessage / GetTask /
    CancelTask over HTTP(S).
  * **Reliability** — per-call timeout + retry with exponential backoff & jitter,
    plus an in-process **loopback fallback** for peers this same process hosts.
  * **Security hook** — an optional token provider adds an `Authorization`
    header per call, supplied by the factory.

The Orchestrator uses this to drive the whole investigation.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from httpx_sse import aconnect_sse

from app.a2a.errors import A2AError
from app.a2a.types import AgentCard, Message, Method, Task
from app.observability.logging import get_logger, log_warning

_log = get_logger("a2a.client")

# Given a target base URL, return a bearer token (or None). Async or sync.
TokenProvider = Callable[[str], "str | Awaitable[str | None] | None"]

# Failures that mean "no connection was ever established" — as opposed to a peer
# that answered slowly or badly. Only these are eligible for the loopback path.
_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)

# Hostnames that can only ever refer to this machine. A peer URL on one of these
# that refuses connections is a *configuration* fault (wrong port, server not up,
# localhost proxied), never a genuine remote outage.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _is_local(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in _LOOPBACK_HOSTS


class A2AClient:
    """A thin, reliable JSON-RPC client for A2A peers."""

    def __init__(
        self,
        *,
        protocol_version: str = "1.0",
        timeout: float = 30.0,
        connect_timeout: float = 5.0,
        max_attempts: int = 3,
        base_delay: float = 0.25,
        max_delay: float = 5.0,
        verify: str | bool = True,
        token_provider: TokenProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        require_signed_cards: bool = False,
        card_verifier: Callable[[AgentCard], bool] | None = None,
    ) -> None:
        self.protocol_version = protocol_version
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.token_provider = token_provider
        self.require_signed_cards = require_signed_cards
        self.card_verifier = card_verifier
        # ``transport`` lets tests point the client at an in-process ASGI app
        # (httpx.ASGITransport) instead of a live socket.
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(timeout, connect=connect_timeout),
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        else:
            client_kwargs["verify"] = verify
        self._client = httpx.AsyncClient(**client_kwargs)
        self._timeout = client_kwargs["timeout"]
        # -- loopback fallback (see set_loopback) --
        self._loopback: httpx.AsyncClient | None = None
        self._loopback_origins: set[str] = set()

    def set_loopback(self, transport: httpx.AsyncBaseTransport) -> None:
        """Register an in-process transport for peers this same process hosts.

        Single-process deployments host every role in one ASGI app, yet the
        Orchestrator still calls its peers over real HTTP — which silently breaks
        the moment the configured ``*_URL`` port stops matching the port the
        server actually bound (a different ``--port``, a proxy that captures
        localhost, the socket not up yet). Rather than degrade every
        investigation over a config typo, a *local* peer that refuses connections
        fails over to this transport: the identical JSON-RPC → auth → validation
        → executor path, minus the socket. Remote peers are untouched — a real
        outage must still degrade loudly.
        """
        self._loopback = httpx.AsyncClient(transport=transport, timeout=self._timeout)

    def uses_loopback(self, url: str) -> bool:
        """True once ``url``'s origin has failed over to the in-process transport."""
        return _origin(url) in self._loopback_origins

    @property
    def loopback_origins(self) -> list[str]:
        """Origins currently served in-process — surfaced by ``/healthz``."""
        return sorted(self._loopback_origins)

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._loopback is not None:
            await self._loopback.aclose()

    async def __aenter__(self) -> A2AClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -- headers ----------------------------------------------------------
    async def _headers(self, base_url: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "A2A-Version": self.protocol_version,
        }
        if self.token_provider is not None:
            token = self.token_provider(base_url)
            if asyncio.iscoroutine(token):
                token = await token
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    # -- discovery --------------------------------------------------------
    async def discover(self, base_url: str) -> AgentCard:
        """Fetch and parse a peer's Agent Card (Agent Discovery).

        With ``require_signed_cards``, a card that is unsigned or fails signature
        verification is refused — rogue-agent protection.
        """
        url = f"{base_url.rstrip('/')}/.well-known/agent-card.json"
        resp = await self._request_with_retry("GET", url,
                                               headers=await self._headers(base_url))
        card = AgentCard.model_validate(resp.json())
        if self.require_signed_cards:
            if self.card_verifier is None or not self.card_verifier(card):
                raise A2AError(-32603,
                               f"agent card for {base_url} failed signature verification")
        return card

    # -- SendMessage (non-streaming) --------------------------------------
    async def send_message(self, base_url: str, message: Message) -> Task:
        result = await self._rpc(base_url, Method.SEND_MESSAGE,
                                 {"message": message.to_wire()})
        return Task.model_validate(result)

    # -- GetTask ----------------------------------------------------------
    async def get_task(self, base_url: str, task_id: str) -> Task:
        result = await self._rpc(base_url, Method.GET_TASK, {"id": task_id})
        return Task.model_validate(result)

    # -- CancelTask -------------------------------------------------------
    async def cancel_task(self, base_url: str, task_id: str) -> Task:
        result = await self._rpc(base_url, Method.CANCEL_TASK, {"id": task_id})
        return Task.model_validate(result)

    # -- SendStreamingMessage (SSE) --------------------------------------
    async def stream_message(self, base_url: str, message: Message
                             ) -> AsyncIterator[dict[str, Any]]:
        """Yield StreamResponse frames: {task}, {statusUpdate}, {artifactUpdate}."""
        body = _rpc_body(Method.SEND_STREAMING_MESSAGE, {"message": message.to_wire()})
        headers = await self._headers(base_url)
        async with aconnect_sse(self._client, "POST", base_url,
                                json=body, headers=headers) as event_source:
            async for sse in event_source.aiter_sse():
                if not sse.data:
                    continue
                frame = _json_loads(sse.data)
                if "error" in frame:
                    err = frame["error"]
                    raise A2AError(err.get("code", -32603), err.get("message", "error"),
                                   err.get("data"))
                yield frame.get("result", {})

    # -- core JSON-RPC with retry ----------------------------------------
    async def _rpc(self, base_url: str, method: str, params: dict) -> dict:
        body = _rpc_body(method, params)
        headers = await self._headers(base_url)
        resp = await self._request_with_retry("POST", base_url, json=body, headers=headers)
        payload = resp.json()
        if "error" in payload:
            err = payload["error"]
            raise A2AError(err.get("code", -32603), err.get("message", "error"),
                           err.get("data"))
        return payload.get("result", {})

    async def _request_with_retry(self, method: str, url: str, **kwargs: Any
                                  ) -> httpx.Response:
        """Retry transient failures (network errors, 5xx, 429) with backoff.

        A2A *application* errors (a JSON-RPC ``error`` in a 200 body) are NOT
        retried here — they are deterministic and handled by the caller.

        A local peer that cannot be connected to fails over to the in-process
        transport (see :meth:`set_loopback`) instead of burning every attempt on
        a socket that will never open.
        """
        if self._can_loopback(url) and _origin(url) in self._loopback_origins:
            return await self._send(self._loopback, method, url, **kwargs)  # type: ignore[arg-type]

        last_exc: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                resp = await self._client.request(method, url, **kwargs)
            except _CONNECT_ERRORS as exc:
                if self._can_loopback(url):
                    return await self._failover(method, url, exc, **kwargs)
                last_exc = exc  # remote peer unreachable → retryable
            except httpx.TransportError as exc:
                last_exc = exc  # network error → retryable
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = httpx.HTTPStatusError("retryable", request=resp.request,
                                                     response=resp)  # retryable
                else:
                    # Deterministic client error (401/403/404/400) — do NOT retry.
                    raise A2AError(-32603,
                                   f"peer returned HTTP {resp.status_code}: {resp.text[:200]}",
                                   data={"status": resp.status_code})
            if attempt == self.max_attempts - 1:
                break
            await asyncio.sleep(self._backoff(attempt))
        raise A2AError(-32603, f"peer request failed after {self.max_attempts} attempts: "
                               f"{last_exc}")

    def _can_loopback(self, url: str) -> bool:
        return self._loopback is not None and _is_local(url)

    async def _failover(self, method: str, url: str, exc: Exception, **kwargs: Any
                        ) -> httpx.Response:
        """Serve a local peer in-process after its socket refused the connection."""
        origin = _origin(url)
        if origin not in self._loopback_origins:
            self._loopback_origins.add(origin)
            log_warning(_log, "a2a_peer_loopback_failover", origin=origin,
                        error=str(exc),
                        detail="peer is not listening at its configured URL; serving "
                               "it in-process. Check the *_URL settings against the "
                               "port the server actually bound.")
        return await self._send(self._loopback, method, url, **kwargs)  # type: ignore[arg-type]

    @staticmethod
    async def _send(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
                    ) -> httpx.Response:
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            raise A2AError(-32603, f"in-process peer request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise A2AError(-32603,
                           f"peer returned HTTP {resp.status_code}: {resp.text[:200]}",
                           data={"status": resp.status_code})
        return resp

    def _backoff(self, attempt: int) -> float:
        # exponential backoff with full jitter
        ceiling = min(self.max_delay, self.base_delay * (2 ** attempt))
        return random.uniform(0, ceiling)  # noqa: S311 — jitter, not crypto


def _rpc_body(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": _next_id(), "method": method, "params": params}


_counter = 0


def _next_id() -> int:
    global _counter
    _counter += 1
    return _counter


def _json_loads(s: str) -> dict:
    import json
    return json.loads(s)
