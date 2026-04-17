"""Tests for glados.ha.ws_client.

Uses a fake `websockets.connect` that plays a scripted server. Each
test drives the client through auth -> subscribe -> get_states ->
optional state events -> clean close.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import pytest

from glados.ha.entity_cache import EntityCache
from glados.ha import ws_client as ws_client_module
from glados.ha.ws_client import HAClient


# ---------------------------------------------------------------------------
# Fake WebSocket
# ---------------------------------------------------------------------------

class _FakeWebSocket:
    """Scripted WS peer. Thread-safe via a threading Lock + Condition
    so the test's main thread can push frames while the client's
    asyncio loop awaits them. Avoids asyncio.Queue (loop-bound in
    Python 3.10+) so the test driver can produce from any thread."""

    def __init__(self) -> None:
        self._buf: list[str] = []
        self._cond = threading.Condition()
        self.sent: list[dict[str, Any]] = []
        self._closed = False

    # Client-facing API used by the HAClient.
    async def send(self, data: str) -> None:
        if self._closed:
            raise RuntimeError("closed")
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        # Poll with short sleeps — keeps the asyncio loop responsive
        # and avoids cross-thread condition-variable gymnastics.
        while True:
            with self._cond:
                if self._buf:
                    return self._buf.pop(0)
                if self._closed:
                    raise RuntimeError("closed")
            await asyncio.sleep(0.01)

    def __aiter__(self) -> "_FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        while True:
            with self._cond:
                if self._buf:
                    item = self._buf.pop(0)
                    if item == "__CLOSE__":
                        raise StopAsyncIteration
                    return item
                if self._closed:
                    raise StopAsyncIteration
            await asyncio.sleep(0.01)

    # Test-facing API (called from main thread).
    def push(self, msg: dict[str, Any]) -> None:
        with self._cond:
            self._buf.append(json.dumps(msg))
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._buf.append("__CLOSE__")
            self._cond.notify_all()


class _FakeConnectFactory:
    """Replace websockets.connect with a context manager that yields
    a scripted FakeWebSocket; the test can grab the instance via
    the `current` attribute once the client has entered the context."""

    def __init__(self) -> None:
        self.current: _FakeWebSocket | None = None
        self._entered = threading.Event()

    def __call__(self, *args, **kwargs):
        outer_self = self
        @asynccontextmanager
        async def _ctx():
            fake = _FakeWebSocket()
            outer_self.current = fake
            outer_self._entered.set()
            try:
                yield fake
            finally:
                fake._closed = True

        return _ctx()

    def wait_entered(self, timeout: float = 2.0) -> _FakeWebSocket:
        ok = self._entered.wait(timeout=timeout)
        if not ok or self.current is None:
            raise RuntimeError("fake websocket never entered")
        return self.current


def _drive_handshake(ws: _FakeWebSocket, entities: list[dict[str, Any]]) -> None:
    """Feed the canonical auth -> subscribe -> get_states sequence."""
    ws.push({"type": "auth_required", "ha_version": "2026.4.0"})

    # Wait for auth message from client.
    # Poll synchronously — the fake queue is thread-safe.
    _wait_for(ws, lambda m: m.get("type") == "auth")
    ws.push({"type": "auth_ok"})

    # Subscribe frame (id=1).
    _wait_for(ws, lambda m: m.get("type") == "subscribe_events")
    ws.push({"id": 1, "type": "result", "success": True, "result": None})

    # get_states — client sends this with id >= 2.
    get_states_msg = _wait_for(ws, lambda m: m.get("type") == "get_states")
    ws.push({
        "id": get_states_msg["id"],
        "type": "result",
        "success": True,
        "result": entities,
    })


def _wait_for(ws: _FakeWebSocket, predicate, timeout: float = 2.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    i = 0
    while True:
        while i < len(ws.sent):
            if predicate(ws.sent[i]):
                return ws.sent[i]
            i += 1
        if time.time() > deadline:
            raise TimeoutError("no matching frame sent")
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConnectAndAuth:
    def test_successful_handshake_loads_states(self, monkeypatch) -> None:
        factory = _FakeConnectFactory()
        cache = EntityCache()
        client = HAClient(
            ws_url="ws://fake/api/websocket",
            token="tok",
            entity_cache=cache,
            call_timeout_s=2.0,
            connect_fn=factory,
        )
        client.start()
        try:
            ws = factory.wait_entered(timeout=2.0)
            _drive_handshake(ws, [
                {"entity_id": "light.kitchen", "state": "on",
                 "attributes": {"friendly_name": "Kitchen"}},
                {"entity_id": "lock.front", "state": "locked",
                 "attributes": {"friendly_name": "Front Door"}},
            ])

            assert client.wait_connected(timeout_s=3.0)
            assert cache.size() == 2
            assert cache.get("light.kitchen").state == "on"

            # Confirm client sent auth with the token.
            auth_frame = next(m for m in ws.sent if m.get("type") == "auth")
            assert auth_frame["access_token"] == "tok"
        finally:
            client.shutdown()

    def test_auth_failure_raises_and_reconnects(self) -> None:
        """When auth_invalid comes back, the connection errors out and
        the supervisor backs off — is_connected stays False."""
        factory = _FakeConnectFactory()
        cache = EntityCache()
        client = HAClient(
            ws_url="ws://fake/api/websocket",
            token="badtok",
            entity_cache=cache,
            call_timeout_s=2.0,
            reconnect_max_s=0.5,
            connect_fn=factory,
        )
        client.start()
        try:
            ws = factory.wait_entered(timeout=2.0)
            ws.push({"type": "auth_required", "ha_version": "2026.4.0"})
            _wait_for(ws, lambda m: m.get("type") == "auth")
            ws.push({"type": "auth_invalid", "message": "nope"})

            # Client should never reach connected.
            assert not client.wait_connected(timeout_s=0.5)
        finally:
            client.shutdown()


class TestStateEvents:
    def test_state_changed_event_updates_cache(self, monkeypatch) -> None:
        factory = _FakeConnectFactory()
        cache = EntityCache()
        client = HAClient(
            ws_url="ws://fake/api/websocket",
            token="tok",
            entity_cache=cache,
            call_timeout_s=2.0,
            connect_fn=factory,
        )
        client.start()
        try:
            ws = factory.wait_entered(timeout=2.0)
            _drive_handshake(ws, [
                {"entity_id": "light.kitchen", "state": "off",
                 "attributes": {"friendly_name": "Kitchen"}},
            ])
            assert client.wait_connected(timeout_s=3.0)

            # Push a state_changed event on subscription id=1.
            ws.push({
                "id": 1,
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "light.kitchen",
                        "new_state": {
                            "entity_id": "light.kitchen",
                            "state": "on",
                            "attributes": {"friendly_name": "Kitchen"},
                        },
                    },
                },
            })

            # Poll until cache updates.
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if cache.get("light.kitchen").state == "on":
                    break
                time.sleep(0.01)
            assert cache.get("light.kitchen").state == "on"
        finally:
            client.shutdown()


class TestServiceCalls:
    def test_conversation_process_round_trip(self, monkeypatch) -> None:
        factory = _FakeConnectFactory()
        cache = EntityCache()
        client = HAClient(
            ws_url="ws://fake/api/websocket",
            token="tok",
            entity_cache=cache,
            call_timeout_s=2.0,
            connect_fn=factory,
        )
        client.start()
        try:
            ws = factory.wait_entered(timeout=2.0)
            _drive_handshake(ws, [])
            assert client.wait_connected(timeout_s=3.0)

            # Prepare the response first so it's queued before the request.
            # Instead, respond to the client's send by watching for the
            # conversation/process frame and then pushing a matching result.
            def _responder() -> None:
                req = _wait_for(ws, lambda m: m.get("type") == "conversation/process",
                                timeout=3.0)
                ws.push({
                    "id": req["id"],
                    "type": "result",
                    "success": True,
                    "result": {
                        "response": {
                            "response_type": "action_done",
                            "speech": {"plain": {"speech": "Turned off the lights."}},
                        },
                        "conversation_id": "c1",
                    },
                })

            threading.Thread(target=_responder, daemon=True).start()

            resp = client.conversation_process("turn off kitchen lights")
            assert resp["success"] is True
            assert resp["result"]["response"]["response_type"] == "action_done"
        finally:
            client.shutdown()

    def test_call_service_round_trip(self, monkeypatch) -> None:
        factory = _FakeConnectFactory()
        cache = EntityCache()
        client = HAClient(
            ws_url="ws://fake/api/websocket",
            token="tok",
            entity_cache=cache,
            call_timeout_s=2.0,
            connect_fn=factory,
        )
        client.start()
        try:
            ws = factory.wait_entered(timeout=2.0)
            _drive_handshake(ws, [])
            assert client.wait_connected(timeout_s=3.0)

            def _responder() -> None:
                req = _wait_for(ws, lambda m: m.get("type") == "call_service",
                                timeout=3.0)
                ws.push({"id": req["id"], "type": "result", "success": True,
                         "result": {"context": {"id": "ctx1"}}})

            threading.Thread(target=_responder, daemon=True).start()

            resp = client.call_service(
                domain="light", service="turn_off",
                target={"entity_id": "light.kitchen"},
            )
            assert resp["success"] is True
        finally:
            client.shutdown()
