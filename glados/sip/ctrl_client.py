"""TCP client for baresip's ``ctrl_tcp`` module.

baresip frames JSON messages as netstrings: ``<length>:<json>,``. Both
commands (request/response) and spontaneous events flow over the same
TCP connection.

This client:
- Maintains the TCP connection with exponential-backoff reconnect
- Frames outgoing commands and unframes incoming bytes
- Correlates request → response by a ``token`` field
- Dispatches events to subscribed callbacks

The exact JSON shape of events (field names, payload structure) varies
slightly across baresip versions. We use a permissive dict-based
interface here and let callers reach into the dict by key. The
integration test in Task 13 validates against real baresip output;
discrepancies surface there, not here.

Usage:
    client = CtrlClient(host="127.0.0.1", port=4444)
    client.subscribe("CALL_INCOMING", on_incoming_call)
    client.subscribe("DTMF", on_dtmf)
    await client.connect()
    response = await client.send("dial", "sip:+1234@host")
    ...
    await client.close()
"""
from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger


# Type alias for event subscribers. Events are passed as dict payloads.
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class CtrlClientError(Exception):
    """Raised on protocol violations or transport failures."""


class CtrlClient:
    """Connect to baresip's ctrl_tcp loopback interface.

    Lifecycle:
      ``connect()`` → use → ``close()``.
      Reconnect happens automatically on transport errors while the
      client is active. Pending command futures fail when the
      connection drops; callers should retry application-level.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4444,
        *,
        reconnect: bool = True,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 10.0,
        command_timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._reconnect = reconnect
        self._reconnect_initial_delay = reconnect_initial_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._command_timeout = command_timeout

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._closed = False

        # Token → future for in-flight commands
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # event_type → list[callback]
        self._subscribers: dict[str, list[EventCallback]] = {}
        # Always-fired catch-all subscribers (event_type=None)
        self._catchall: list[EventCallback] = []

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the TCP connection and start the reader loop."""
        self._closed = False
        await self._open_connection()

    async def close(self) -> None:
        """Tear down. Pending commands are cancelled; reconnect stops."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        # Fail any in-flight futures
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CtrlClientError("client closed"))
        self._pending.clear()

    async def _open_connection(self) -> None:
        try:
            self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        except OSError as e:
            raise CtrlClientError(f"failed to connect to {self._host}:{self._port}: {e}") from e
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.bind(group="sip").debug(f"ctrl_tcp: connected to {self._host}:{self._port}")

    @property
    def is_connected(self) -> bool:
        return (
            self._writer is not None
            and not self._writer.is_closing()
            and self._reader is not None
        )

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def subscribe(self, event_type: str | None, callback: EventCallback) -> None:
        """Register a callback for a given event type.

        Pass ``event_type=None`` for a catch-all subscriber that receives
        every event. Useful for debugging / audit logging.

        Multiple subscribers per event are supported; they fire in
        registration order.
        """
        if event_type is None:
            self._catchall.append(callback)
        else:
            self._subscribers.setdefault(event_type, []).append(callback)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(self, command: str, params: str = "") -> dict[str, Any]:
        """Send a command, await the matching response.

        Raises:
            CtrlClientError: if not connected, or response timeout.
        """
        if not self.is_connected:
            raise CtrlClientError("not connected")
        token = secrets.token_hex(8)
        message = {"command": command, "params": params, "token": token}
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[token] = future
        try:
            await self._write_frame(message)
            return await asyncio.wait_for(future, timeout=self._command_timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(token, None)
            raise CtrlClientError(f"command {command!r} timed out") from e
        except Exception:
            self._pending.pop(token, None)
            raise

    async def _write_frame(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        frame = f"{len(body)}:".encode("ascii") + body + b","
        if self._writer is None:
            raise CtrlClientError("writer is None")
        self._writer.write(frame)
        await self._writer.drain()

    # ------------------------------------------------------------------
    # Receiving
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        """Read netstring-framed JSON from the socket; dispatch each.

        Exits cleanly when the connection closes; if reconnect is enabled
        and we haven't been explicitly closed, schedules a reconnect.
        """
        buf = bytearray()
        try:
            while True:
                if self._reader is None:
                    break
                chunk = await self._reader.read(4096)
                if not chunk:
                    break  # EOF
                buf.extend(chunk)
                while True:
                    payload, consumed = self._try_parse_frame(buf)
                    if consumed == 0:
                        break  # need more bytes
                    del buf[:consumed]
                    if payload is not None:
                        await self._dispatch(payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.bind(group="sip").warning(f"ctrl_tcp: read loop error: {e}")
        finally:
            # Connection ended
            if not self._closed and self._reconnect:
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    @staticmethod
    def _try_parse_frame(buf: bytearray) -> tuple[dict[str, Any] | None, int]:
        """Parse one netstring from ``buf``. Returns (payload, bytes_consumed).

        bytes_consumed=0 means insufficient data; caller should read more.
        Raises ``CtrlClientError`` on malformed input.
        """
        # Find the colon
        colon = buf.find(b":")
        if colon < 0:
            return None, 0
        # Length prefix
        try:
            length = int(buf[:colon])
        except ValueError as e:
            raise CtrlClientError(f"malformed netstring length: {bytes(buf[:colon])!r}") from e
        # Need length + body + comma
        total = colon + 1 + length + 1
        if len(buf) < total:
            return None, 0
        if buf[total - 1] != ord(","):
            raise CtrlClientError("malformed netstring: missing trailing comma")
        body = bytes(buf[colon + 1 : colon + 1 + length])
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise CtrlClientError(f"malformed JSON in ctrl_tcp message: {body!r}") from e
        return payload, total

    async def _dispatch(self, payload: dict[str, Any]) -> None:
        """Route a parsed message to either a pending future or subscribers."""
        # Response — has a token we issued
        token = payload.get("token")
        if token is not None and token in self._pending:
            fut = self._pending.pop(token)
            if not fut.done():
                fut.set_result(payload)
            return

        # Event — dispatch to subscribers
        event_type = payload.get("type") or payload.get("event") or ""
        # Specific subscribers
        for cb in self._subscribers.get(event_type, []):
            try:
                await cb(payload)
            except Exception:
                logger.bind(group="sip").exception(f"ctrl_tcp: subscriber for {event_type!r} raised")
        # Catch-all subscribers
        for cb in self._catchall:
            try:
                await cb(payload)
            except Exception:
                logger.bind(group="sip").exception("ctrl_tcp: catch-all subscriber raised")

    async def _reconnect_loop(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        delay = self._reconnect_initial_delay
        while not self._closed:
            logger.bind(group="sip").info(f"ctrl_tcp: reconnecting in {delay:.1f}s")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            if self._closed:
                return
            try:
                await self._open_connection()
                logger.bind(group="sip").success("ctrl_tcp: reconnected")
                return
            except CtrlClientError as e:
                logger.bind(group="sip").debug(f"ctrl_tcp: reconnect failed: {e}")
            delay = min(delay * 2, self._reconnect_max_delay)
