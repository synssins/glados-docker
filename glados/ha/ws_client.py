"""Persistent Home Assistant WebSocket client.

Owns one persistent connection to `ws://ha/api/websocket`. Authenticates,
subscribes to `state_changed`, fetches initial `get_states`, and exposes
a thread-safe `call()` API for issuing any HA WebSocket message
(call_service, conversation/process, get_states) from threaded code.

This is the pattern `glados/mcp/manager.py` already uses: an asyncio
loop runs in a dedicated background thread; threaded callers submit
coroutines via `asyncio.run_coroutine_threadsafe`. One loop per client.

Reconnect policy:
- Exponential backoff, capped at 30s.
- On reconnect, re-auth, re-subscribe, re-run get_states. State change
  events during the disconnect window are lost by design — the resync
  covers correctness.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Callable

from loguru import logger

from .entity_cache import EntityCache

try:
    import websockets
    from websockets.exceptions import ConnectionClosed, WebSocketException
    _WEBSOCKETS_AVAILABLE = True
except ImportError:  # pragma: no cover
    websockets = None
    ConnectionClosed = Exception
    WebSocketException = Exception
    _WEBSOCKETS_AVAILABLE = False


# The HA WebSocket protocol auto-increments message IDs starting from 1.
# Each request must carry a unique id; responses echo that id.
_INITIAL_MESSAGE_ID = 2  # 1 is reserved for subscribe_events at startup.


class HAClient:
    """Single persistent HA WebSocket connection with reconnect.

    Use `start()` once at process startup; it returns immediately. The
    background thread handles auth, subscription, and the read loop.

    From threaded code, call `call(msg)` to issue any HA message and
    await the response. From async code already on the client's loop,
    call `acall(msg)` instead.
    """

    def __init__(
        self,
        ws_url: str,
        token: str,
        entity_cache: EntityCache,
        reconnect_max_s: float = 30.0,
        call_timeout_s: float = 10.0,
        connect_fn: Callable[..., Any] | None = None,
    ) -> None:
        # `connect_fn` is injected by tests (a fake `websockets.connect`).
        # Production callers leave it None and we use the real library.
        if connect_fn is None and not _WEBSOCKETS_AVAILABLE:
            raise RuntimeError(
                "HAClient requires the `websockets` package; add it to "
                "pyproject.toml dependencies."
            )
        self._ws_url = ws_url
        self._token = token
        self._cache = entity_cache
        self._reconnect_max_s = reconnect_max_s
        self._call_timeout_s = call_timeout_s
        self._connect_fn = connect_fn or (
            websockets.connect if _WEBSOCKETS_AVAILABLE else None
        )

        # Async state — only touched from the loop thread.
        self._ws: Any = None
        self._next_id = _INITIAL_MESSAGE_ID
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}

        # Loop management.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._connected_event = threading.Event()

        # Callbacks for observers (used by tests and future audit hooks).
        self._on_state_changed: list[Callable[[dict[str, Any]], None]] = []

    # ── Lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background asyncio loop + connection thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run_loop_forever, name="HAClient", daemon=True
        )
        self._thread.start()

    def shutdown(self, timeout_s: float = 2.0) -> None:
        """Clean shutdown: signal the supervisor, cancel pending tasks,
        let the loop drain naturally. Avoids the "Event loop stopped
        before Future completed" crash that `loop.stop()` mid-flight
        would cause."""
        self._shutdown.set()
        loop = self._loop
        if loop and loop.is_running():
            # Cancel every task running on the loop — the supervisor
            # will see CancelledError and exit cleanly.
            def _cancel_all() -> None:
                for task in asyncio.all_tasks(loop):
                    task.cancel()
            loop.call_soon_threadsafe(_cancel_all)
        if self._thread:
            self._thread.join(timeout=timeout_s)

    def wait_connected(self, timeout_s: float = 5.0) -> bool:
        """Block until the connection is authenticated + state-synced,
        or `timeout_s` passes. Returns True if connected."""
        return self._connected_event.wait(timeout=timeout_s)

    def is_connected(self) -> bool:
        return self._connected_event.is_set()

    # ── Public API (thread-safe) ─────────────────────────────

    def call(self, msg: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
        """Blocking call into HA. Must NOT be invoked from the client's
        own asyncio loop (use `acall` there). Raises TimeoutError or
        RuntimeError if not connected."""
        if self._loop is None:
            raise RuntimeError("HAClient not started")
        if not self.is_connected():
            raise RuntimeError("HAClient not connected")
        fut = asyncio.run_coroutine_threadsafe(self.acall(msg), self._loop)
        return fut.result(timeout=timeout_s or self._call_timeout_s)

    async def acall(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Async version: same as `call` but for callers already on the
        client's loop."""
        if self._ws is None:
            raise RuntimeError("HAClient not connected")
        msg_id = self._next_id
        self._next_id += 1
        payload = {**msg, "id": msg_id}
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send(json.dumps(payload))
            return await asyncio.wait_for(fut, timeout=self._call_timeout_s)
        finally:
            self._pending.pop(msg_id, None)

    # ── Convenience helpers ──────────────────────────────────

    def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "type": "call_service",
            "domain": domain,
            "service": service,
        }
        if service_data:
            msg["service_data"] = service_data
        if target:
            msg["target"] = target
        return self.call(msg, timeout_s=timeout_s)

    def conversation_process(
        self,
        text: str,
        language: str = "en",
        conversation_id: str | None = None,
        agent_id: str | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "type": "conversation/process",
            "text": text,
            "language": language,
        }
        if conversation_id:
            msg["conversation_id"] = conversation_id
        if agent_id:
            msg["agent_id"] = agent_id
        return self.call(msg, timeout_s=timeout_s)

    # ── Internals ────────────────────────────────────────────

    def _run_loop_forever(self) -> None:
        """Thread target: own the asyncio loop and the reconnect loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._reconnect_supervisor())
        except asyncio.CancelledError:
            # Clean shutdown path — supervisor was cancelled by shutdown().
            pass
        except Exception as exc:
            logger.exception("HAClient loop crashed: {}", exc)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _reconnect_supervisor(self) -> None:
        backoff_s = 1.0
        while not self._shutdown.is_set():
            try:
                await self._connect_and_serve()
                # Clean disconnect — reset backoff.
                backoff_s = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("HA WS connection error: {}; retrying in {:.1f}s",
                               exc, backoff_s)
            self._connected_event.clear()
            if self._shutdown.is_set():
                break
            await asyncio.sleep(backoff_s)
            backoff_s = min(self._reconnect_max_s, backoff_s * 2)

    async def _connect_and_serve(self) -> None:
        logger.info("HA WS connecting to {}", self._ws_url)
        async with self._connect_fn(self._ws_url, max_size=None) as ws:
            self._ws = ws
            self._next_id = _INITIAL_MESSAGE_ID
            self._pending.clear()
            reader_task: asyncio.Task[None] | None = None
            try:
                # Auth + subscribe use direct recv — no reader running yet,
                # and HA responds to these in strict order.
                await self._authenticate(ws)
                await self._subscribe_state_changed(ws)
                # Start the read loop concurrently. Every subsequent call
                # (get_states, call_service, conversation/process) uses
                # `acall` which submits a request and awaits a future that
                # the reader resolves when a matching `result` frame
                # arrives. Without this concurrency, `_load_initial_states`
                # would deadlock waiting for its own response.
                reader_task = asyncio.create_task(self._read_loop(ws))
                await self._load_initial_states()
                self._connected_event.set()
                logger.success(
                    "HA WS connected; cache has {} entities",
                    self._cache.size(),
                )
                # Block until the reader task finishes (connection closed,
                # error, or shutdown cancellation).
                await reader_task
            finally:
                self._connected_event.clear()
                self._ws = None
                if reader_task and not reader_task.done():
                    reader_task.cancel()

    async def _read_loop(self, ws: Any) -> None:
        """Run until the WS iterator exits. Dispatches every frame."""
        try:
            async for raw in ws:
                await self._dispatch_incoming(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("HA WS read loop ended: {}", exc)

    def _refresh_token(self) -> None:
        """Re-read the HA token from the live config store before each
        (re)connect. Without this the singleton HAClient keeps the
        token it was constructed with at server boot — a WebUI save
        rotates the token on disk but this in-process client would
        keep retrying with the stale value until the container
        restarts. Live incident 2026-04-23."""
        try:
            from glados.core.config_store import cfg
            current = cfg.ha_token
            if current and current != self._token:
                logger.info("HA WS: detected rotated token; refreshing in-process value")
                self._token = current
        except Exception as exc:
            logger.debug("HA WS: token refresh lookup skipped ({})", exc)

    async def _authenticate(self, ws: Any) -> None:
        # HA sends auth_required first.
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"HA did not request auth (got {msg!r})")
        # Pull the freshest token from the config store every auth
        # handshake so rotation-via-WebUI takes effect on the next
        # reconnect attempt without a container restart.
        self._refresh_token()
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        raw = await ws.recv()
        resp = json.loads(raw)
        if resp.get("type") != "auth_ok":
            raise RuntimeError(f"HA auth failed: {resp!r}")

    async def _subscribe_state_changed(self, ws: Any) -> None:
        # ID 1 is reserved for this subscription; all call responses
        # use ids >= _INITIAL_MESSAGE_ID (2+).
        await ws.send(json.dumps({
            "id": 1,
            "type": "subscribe_events",
            "event_type": "state_changed",
        }))
        raw = await ws.recv()
        resp = json.loads(raw)
        if not resp.get("success"):
            raise RuntimeError(f"subscribe_events failed: {resp!r}")

    async def _load_initial_states(self) -> None:
        resp = await self.acall({"type": "get_states"})
        if not resp.get("success"):
            raise RuntimeError(f"get_states failed: {resp!r}")
        loaded = self._cache.apply_get_states(resp.get("result") or [])
        logger.info("HA WS initial get_states loaded {} entities", loaded)
        # Phase 8.1: follow up with the entity_registry so each entity
        # carries its device_id. Required for light/switch twin dedup
        # in the candidate scorer — `get_states` does not expose
        # device_id. Failure is logged and ignored: dedup degrades to
        # a no-op without a registry, which is the pre-8.1 behaviour.
        try:
            reg = await self.acall({"type": "config/entity_registry/list"})
            if reg.get("success"):
                updated = self._cache.apply_entity_registry(reg.get("result") or [])
                logger.info("HA WS entity_registry applied; device_id on {} entities", updated)
            else:
                logger.warning("HA WS entity_registry/list returned failure: {}", reg)
        except Exception as exc:
            logger.warning("HA WS entity_registry/list raised: {}", exc)

    async def _dispatch_incoming(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("HA WS: dropped malformed frame")
            return

        msg_type = msg.get("type")
        msg_id = msg.get("id")

        if msg_type == "event" and msg_id == 1:
            event = msg.get("event") or {}
            if event.get("event_type") == "state_changed":
                data = event.get("data") or {}
                self._cache.apply_state_changed(data)
                for cb in self._on_state_changed:
                    try:
                        cb(data)
                    except Exception as exc:
                        logger.warning("state_changed callback raised: {}", exc)
            return

        if msg_type == "result" and msg_id is not None:
            fut = self._pending.get(msg_id)
            if fut and not fut.done():
                fut.set_result(msg)
            return

        # Anything else — log and move on.
        logger.trace("HA WS unhandled frame: {}", msg_type)

    # ── Hooks for tests / observability ──────────────────────

    def on_state_changed(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback invoked for every state_changed event.
        Callbacks run on the asyncio loop thread; keep them fast."""
        self._on_state_changed.append(cb)

    def off_state_changed(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Remove a previously-registered state_changed callback.
        Safe when the callback isn't registered — silently skips."""
        try:
            self._on_state_changed.remove(cb)
        except ValueError:
            pass
