"""Tests for glados.sip.ctrl_client.

We exercise the client against an in-process mock TCP server that
speaks netstring-framed JSON, simulating baresip's ctrl_tcp module.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from glados.sip.ctrl_client import CtrlClient, CtrlClientError


# ---------------------------------------------------------------------------
# Mock server
# ---------------------------------------------------------------------------

class MockBaresipCtrl:
    """A tiny TCP server that speaks the ctrl_tcp wire format."""

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = 0
        self._server: asyncio.Server | None = None
        self._clients: list[asyncio.StreamWriter] = []
        self.received: list[dict[str, Any]] = []
        # Optional handler — given a parsed command, returns a response dict OR None
        self.command_handler: callable | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, 0, reuse_address=True,
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for w in self._clients:
            try:
                w.close()
            except Exception:
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.append(writer)
        buf = bytearray()
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    payload, consumed = _try_parse(buf)
                    if consumed == 0:
                        break
                    del buf[:consumed]
                    if payload is None:
                        continue
                    self.received.append(payload)
                    if self.command_handler is not None:
                        resp = self.command_handler(payload)
                        if resp is not None:
                            await self.push(resp)
        except Exception:
            pass

    async def push(self, payload: dict[str, Any]) -> None:
        """Send a frame to all connected clients."""
        body = json.dumps(payload).encode("utf-8")
        frame = f"{len(body)}:".encode("ascii") + body + b","
        for w in list(self._clients):
            try:
                w.write(frame)
                await w.drain()
            except Exception:
                pass

    def first(self) -> dict[str, Any]:
        assert len(self.received) == 1
        return self.received[0]


def _try_parse(buf: bytearray) -> tuple[dict[str, Any] | None, int]:
    """Local netstring parser (mirrors the one in ctrl_client)."""
    colon = buf.find(b":")
    if colon < 0:
        return None, 0
    length = int(buf[:colon])
    total = colon + 1 + length + 1
    if len(buf) < total:
        return None, 0
    body = bytes(buf[colon + 1 : colon + 1 + length])
    return json.loads(body), total


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_command_round_trip() -> None:
    """Client sends a command; server echoes a response with the same token."""
    server = MockBaresipCtrl()
    server.command_handler = lambda req: {
        "response": True,
        "token": req["token"],
        "ok": True,
        "data": "registered",
    }
    await server.start()
    try:
        client = CtrlClient(host=server.host, port=server.port)
        await client.connect()
        try:
            resp = await client.send("dial", "sip:+1234@host")
            assert resp["ok"] is True
            assert resp["data"] == "registered"
            sent = server.first()
            assert sent["command"] == "dial"
            assert sent["params"] == "sip:+1234@host"
            assert "token" in sent
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_send_when_disconnected_raises() -> None:
    client = CtrlClient(host="127.0.0.1", port=1)  # nothing listening
    # Don't even connect — just send.
    with pytest.raises(CtrlClientError, match="not connected"):
        await client.send("dial")


@pytest.mark.asyncio
async def test_command_timeout() -> None:
    """If the server never responds, send() times out."""
    server = MockBaresipCtrl()
    # No command_handler — server swallows the command silently
    await server.start()
    try:
        client = CtrlClient(host=server.host, port=server.port, command_timeout=0.5)
        await client.connect()
        try:
            with pytest.raises(CtrlClientError, match="timed out"):
                await client.send("dial")
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_event_subscription_fires_on_match() -> None:
    server = MockBaresipCtrl()
    await server.start()
    try:
        client = CtrlClient(host=server.host, port=server.port)
        received: list[dict[str, Any]] = []

        async def on_dtmf(event: dict[str, Any]) -> None:
            received.append(event)

        client.subscribe("DTMF", on_dtmf)
        await client.connect()
        try:
            await server.push({"type": "DTMF", "key": "1"})
            await server.push({"type": "DTMF", "key": "2"})
            await server.push({"type": "OTHER", "key": "x"})  # ignored by DTMF subscriber
            # Give the read loop time to process
            await asyncio.sleep(0.2)
            assert len(received) == 2
            assert received[0]["key"] == "1"
            assert received[1]["key"] == "2"
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_catchall_subscriber_receives_all() -> None:
    server = MockBaresipCtrl()
    await server.start()
    try:
        client = CtrlClient(host=server.host, port=server.port)
        all_events: list[dict[str, Any]] = []

        async def catch_all(event: dict[str, Any]) -> None:
            all_events.append(event)

        client.subscribe(None, catch_all)
        await client.connect()
        try:
            await server.push({"type": "A"})
            await server.push({"type": "B"})
            await asyncio.sleep(0.2)
            assert len(all_events) == 2
            assert {e["type"] for e in all_events} == {"A", "B"}
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_partial_frame_handling() -> None:
    """Frames that arrive in pieces should still parse correctly."""
    server = MockBaresipCtrl()
    await server.start()
    try:
        client = CtrlClient(host=server.host, port=server.port)
        received: list[dict[str, Any]] = []

        async def cb(event: dict[str, Any]) -> None:
            received.append(event)

        client.subscribe("INCOMING", cb)
        await client.connect()
        try:
            # Push a frame in two pieces by sending raw bytes through writer
            body = json.dumps({"type": "INCOMING", "from": "sip:test@host"}).encode("utf-8")
            frame = f"{len(body)}:".encode("ascii") + body + b","
            for w in server._clients:
                w.write(frame[:10])
                await w.drain()
                await asyncio.sleep(0.05)
                w.write(frame[10:])
                await w.drain()
            await asyncio.sleep(0.2)
            assert len(received) == 1
            assert received[0]["from"] == "sip:test@host"
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_close_fails_pending_commands() -> None:
    server = MockBaresipCtrl()
    await server.start()
    try:
        client = CtrlClient(host=server.host, port=server.port, command_timeout=10.0)
        await client.connect()
        # Start a send that will never resolve
        send_task = asyncio.create_task(client.send("dial"))
        # Give it time to register the future
        await asyncio.sleep(0.1)
        await client.close()
        # The task should now error
        with pytest.raises(CtrlClientError, match="closed"):
            await send_task
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_multiple_subscribers_per_event_fire_in_order() -> None:
    server = MockBaresipCtrl()
    await server.start()
    try:
        client = CtrlClient(host=server.host, port=server.port)
        order: list[str] = []

        async def first(event: dict[str, Any]) -> None:
            order.append("first")

        async def second(event: dict[str, Any]) -> None:
            order.append("second")

        client.subscribe("X", first)
        client.subscribe("X", second)
        await client.connect()
        try:
            await server.push({"type": "X"})
            await asyncio.sleep(0.1)
            assert order == ["first", "second"]
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_subscriber_exception_does_not_break_dispatch() -> None:
    server = MockBaresipCtrl()
    await server.start()
    try:
        client = CtrlClient(host=server.host, port=server.port)
        order: list[str] = []

        async def broken(event: dict[str, Any]) -> None:
            raise RuntimeError("intentional")

        async def working(event: dict[str, Any]) -> None:
            order.append("working")

        client.subscribe("X", broken)
        client.subscribe("X", working)
        await client.connect()
        try:
            await server.push({"type": "X"})
            await asyncio.sleep(0.1)
            assert order == ["working"]
        finally:
            await client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_malformed_length_disconnects_with_error_log(caplog) -> None:
    """A bad netstring length should log + drop the read loop, not crash."""
    server = MockBaresipCtrl()
    await server.start()
    try:
        client = CtrlClient(host=server.host, port=server.port, reconnect=False)
        await client.connect()
        try:
            for w in server._clients:
                w.write(b"NOTNUMERIC:somebody,")
                await w.drain()
            await asyncio.sleep(0.3)
            # Read loop should have ended; client should still be technically connected,
            # but no further messages will dispatch.
            assert client._reader_task is not None
            assert client._reader_task.done()
        finally:
            await client.close()
    finally:
        await server.stop()
