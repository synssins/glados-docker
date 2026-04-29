import asyncio
import fnmatch
import os
import subprocess
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from .config import MCPServerConfig
from ..observability import ObservabilityBus, trim_message

try:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client
except ImportError:  # pragma: no cover - handled in runtime checks
    ClientSession = None  # type: ignore[assignment]
    sse_client = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    streamable_http_client = None  # type: ignore[assignment]


class MCPError(RuntimeError):
    pass


class MCPToolError(MCPError):
    pass


@dataclass(frozen=True)
class MCPToolEntry:
    server: str
    name: str
    description: str | None
    input_schema: dict[str, Any] | None


@dataclass
class _ResourceCacheEntry:
    message: dict[str, str]
    expires_at: float


def _rotate_log_if_needed(path: Path, max_bytes: int = 1 * 1024 * 1024) -> None:
    """If `path` exists and is over `max_bytes`, rename to `path.1`
    (overwriting any prior backup) so the next session starts a fresh log."""
    try:
        if not path.exists():
            return
        if path.stat().st_size <= max_bytes:
            return
        backup = path.with_suffix(path.suffix + ".1")
        if backup.exists():
            backup.unlink()
        path.rename(backup)
    except OSError as exc:
        logger.warning("log rotation failed for {!s}: {}", path, exc)


class MCPManager:
    def __init__(
        self,
        servers: Iterable[MCPServerConfig],
        tool_timeout: float = 30.0,
        observability_bus: ObservabilityBus | None = None,
    ) -> None:
        self._servers = {server.name: server for server in servers}
        self._tool_timeout = tool_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run_loop, name="MCPManager", daemon=True)
        self._ready = threading.Event()
        self._shutdown_event = threading.Event()
        self._shutdown_async: asyncio.Event | None = None
        self._observability_bus = observability_bus

        self._servers_lock = threading.Lock()

        self._tool_lock = threading.Lock()
        self._tool_registry: dict[str, MCPToolEntry] = {}

        self._resource_lock = threading.Lock()
        self._resource_cache: dict[tuple[str, str], _ResourceCacheEntry] = {}
        self._resource_refreshing: set[tuple[str, str]] = set()

        self._session_tasks: dict[str, asyncio.Task[None]] = {}
        self._sessions: dict[str, ClientSession] = {}

        # Phase 2b: per-plugin event ring (connect/disconnect/error/tools).
        self._plugin_events: dict[str, deque[dict]] = defaultdict(
            lambda: deque(maxlen=256)
        )
        self._plugin_events_lock = threading.Lock()

        # Phase 2b: log dir for stdio plugin stderr.
        self._plugin_log_dir = Path(
            os.environ.get("GLADOS_PLUGIN_LOG_DIR", "/app/logs/plugins")
        )

    def start(self) -> None:
        if ClientSession is None:
            # If the mcp library isn't installed but no servers are configured,
            # this is a no-op. Phase 2b add_server() will surface the error
            # later if a server is actually registered.
            if not self._servers:
                return
            raise MCPError("MCP client library is not installed. Install the 'mcp' package to enable MCP support.")
        if self._thread.is_alive():
            return
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise MCPError("MCP manager failed to initialize in time.")

    def shutdown(self) -> None:
        if not self._thread.is_alive() or self._loop is None:
            return
        self._shutdown_event.set()
        if self._shutdown_async:
            self._loop.call_soon_threadsafe(self._shutdown_async.set)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)

    def add_server(self, config: MCPServerConfig) -> None:
        """Thread-safe. Register a new server and schedule its session
        runner on the manager's loop. Raises if a server with the same
        name is already registered."""
        if self._loop is None:
            raise MCPError("MCP manager is not running.")
        with self._servers_lock:
            if config.name in self._servers:
                raise MCPError(f"MCP server '{config.name}' is already registered.")
            self._servers[config.name] = config

        spawned = threading.Event()

        def _spawn() -> None:
            assert self._loop is not None
            try:
                task = self._loop.create_task(self._session_runner(config))
                self._session_tasks[config.name] = task
            finally:
                spawned.set()

        self._loop.call_soon_threadsafe(_spawn)
        # Wait briefly for the task to register; the loop should run _spawn
        # almost immediately. If the loop is dead this returns False and the
        # caller will see no task in _session_tasks.
        spawned.wait(timeout=2.0)

    def remove_server(self, name: str, timeout: float = 5.0) -> None:
        """Thread-safe. Cancel the session task for `name`, await up to
        `timeout` seconds, drop from internal state. No-op if missing."""
        if self._loop is None:
            return
        if name not in self._servers:
            return

        task = self._session_tasks.get(name)
        future: asyncio.Future | None = None

        def _cancel() -> None:
            if task and not task.done():
                task.cancel()

        self._loop.call_soon_threadsafe(_cancel)

        if task is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._await_task(task), self._loop,
                )
                future.result(timeout=timeout)
            except FuturesTimeoutError:
                logger.warning(
                    "MCP: remove_server({!r}) cancellation did not finish in {} s; orphaning",
                    name, timeout,
                )
            except Exception as exc:
                logger.warning(
                    "MCP: remove_server({!r}) cleanup raised: {}", name, exc,
                )

        self._sessions.pop(name, None)
        self._session_tasks.pop(name, None)
        with self._servers_lock:
            self._servers.pop(name, None)
        self._remove_tools_for_server(name)
        self._clear_resource_cache(name)

    @staticmethod
    async def _await_task(task: asyncio.Task) -> None:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("MCP: task cleanup raised: {}", exc)

    def get_tool_definitions(
        self,
        server_filter: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        # server_filter lets the chitchat-path plugin gate advertise
        # only matched plugins' tools (Phase 2c) instead of dumping the
        # whole catalog into every prompt. Default None preserves the
        # existing legacy HA-gate behavior of returning every tool.
        with self._tool_lock:
            entries = list(self._tool_registry.items())
        entries.sort(key=lambda item: item[0])
        if server_filter is not None:
            allow = set(server_filter)
            entries = [(name, e) for (name, e) in entries if e.server in allow]
        return [self._tool_entry_to_definition(tool_name, entry) for tool_name, entry in entries]

    def get_context_messages(self, timeout: float = 5.0, block: bool = True) -> list[dict[str, str]]:
        if not self._servers:
            return []
        messages: list[dict[str, str]] = []
        for server in self._servers.values():
            if not server.context_resources:
                continue
            for uri in server.context_resources:
                cached = self._get_cached_resource(server.name, uri, allow_expired=not block)
                if cached:
                    messages.append(cached.message)
                    if cached.expires_at < time.time():
                        self._schedule_resource_refresh(server.name, uri, server.resource_ttl_s)
                    continue
                if not block:
                    self._schedule_resource_refresh(server.name, uri, server.resource_ttl_s)
                    continue
                fetched = self._fetch_resource(server.name, uri, timeout)
                if fetched:
                    self._cache_resource(server.name, uri, fetched, server.resource_ttl_s)
                    messages.append(fetched)
        return messages

    def call_tool(self, tool_name: str, arguments: dict[str, Any], timeout: float | None = None) -> str:
        server_name, local_tool = self._parse_tool_name(tool_name)
        if self._loop is None:
            raise MCPError("MCP manager is not running.")
        future = asyncio.run_coroutine_threadsafe(
            self._call_tool_async(server_name, local_tool, arguments),
            self._loop,
        )
        try:
            return future.result(timeout=timeout or self._tool_timeout)
        except FuturesTimeoutError as exc:
            future.cancel()
            raise MCPError(f"MCP tool '{tool_name}' timed out.") from exc
        except MCPError:
            raise
        except Exception as exc:
            future.cancel()
            raise MCPError(str(exc)) from exc

    def status_snapshot(self) -> list[dict[str, Any]]:
        with self._tool_lock:
            tools = list(self._tool_registry.values())
        with self._resource_lock:
            resource_counts: dict[str, int] = {}
            for (server_name, _uri) in self._resource_cache.keys():
                resource_counts[server_name] = resource_counts.get(server_name, 0) + 1
        connected = set(self._sessions.keys())
        tool_counts: dict[str, int] = {}
        for entry in tools:
            tool_counts[entry.server] = tool_counts.get(entry.server, 0) + 1
        entries: list[dict[str, Any]] = []
        for server in self._servers.values():
            entries.append(
                {
                    "name": server.name,
                    "connected": server.name in connected,
                    "tools": tool_counts.get(server.name, 0),
                    "resources": resource_counts.get(server.name, 0),
                    "context_resources": len(server.context_resources or []),
                }
            )
        entries.sort(key=lambda item: item["name"])
        return entries

    def _record_event(self, server_name: str, *, kind: str, message: str,
                      level: str = "info", meta: dict | None = None) -> None:
        """Append an event to the per-plugin ring. Also bridges to the
        ObservabilityBus when one is configured."""
        entry = {
            "ts": time.time(),
            "kind": kind,
            "level": level,
            "message": message,
            "meta": meta or {},
        }
        with self._plugin_events_lock:
            self._plugin_events[server_name].append(entry)
        if self._observability_bus:
            self._observability_bus.emit(
                source="mcp",
                kind=kind,
                message=message,
                meta={"server": server_name, **(meta or {})},
                level=level,
            )

    def get_plugin_events(self, server_name: str, limit: int = 200) -> list[dict]:
        """Return up to `limit` recent events for `server_name`, oldest-first."""
        with self._plugin_events_lock:
            buf = self._plugin_events.get(server_name)
            if not buf:
                return []
            return list(buf)[-limit:]

    def _run_loop(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._shutdown_async = asyncio.Event()
        self._loop.create_task(self._initialize_servers())
        self._ready.set()
        self._loop.run_forever()

        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    async def _initialize_servers(self) -> None:
        for server in self._servers.values():
            self._session_tasks[server.name] = asyncio.create_task(self._session_runner(server))

    async def _session_runner(self, config: MCPServerConfig) -> None:
        retry_delay = 2.0
        while not self._shutdown_event.is_set() and self._shutdown_async:
            try:
                async with self._open_transport(config) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        self._sessions[config.name] = session
                        self._record_event(
                            config.name, kind="connect",
                            message=f"{config.name} connected",
                            meta={"transport": config.transport},
                        )
                        await self._refresh_tools(config, session)
                        await self._refresh_resources(config, session)
                        await self._shutdown_async.wait()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                detail = str(exc)
                if hasattr(exc, 'exceptions'):
                    detail += " | sub-exceptions: " + "; ".join(str(e) for e in exc.exceptions)
                logger.warning(f"MCP: server '{config.name}' connection failed: {detail}")
                self._record_event(
                    config.name, kind="error", level="warning",
                    message=trim_message(f"{config.name} failed: {exc}"),
                )
                await asyncio.sleep(retry_delay)
            finally:
                self._sessions.pop(config.name, None)
                self._remove_tools_for_server(config.name)
                self._clear_resource_cache(config.name)
                self._record_event(
                    config.name, kind="disconnect",
                    message=f"{config.name} disconnected",
                )

    @asynccontextmanager
    async def _open_transport(self, config: MCPServerConfig):
        if config.transport == "stdio":
            if not config.command:
                raise MCPError(f"MCP server '{config.name}' requires a command for stdio transport.")
            params = StdioServerParameters(command=config.command, args=config.args, env=config.env)

            # Phase 2b: per-plugin stderr log file with size-cap rotation.
            # Falls back to DEVNULL if the log dir isn't writable.
            log_fd = subprocess.DEVNULL
            try:
                self._plugin_log_dir.mkdir(parents=True, exist_ok=True)
                log_path = self._plugin_log_dir / f"{config.name}.log"
                _rotate_log_if_needed(log_path)
                log_fd = open(log_path, "ab", buffering=0)
            except OSError as exc:
                logger.warning(
                    "MCP: cannot open plugin log {!s}; using DEVNULL: {}",
                    config.name, exc,
                )

            try:
                async with stdio_client(params, errlog=log_fd) as streams:
                    yield streams
                    return
            finally:
                if log_fd is not subprocess.DEVNULL:
                    try:
                        log_fd.close()
                    except Exception:
                        pass

        if not config.url:
            raise MCPError(f"MCP server '{config.name}' requires a URL for {config.transport} transport.")

        headers = dict(config.headers or {})
        token = config.token
        if not token:
            # Fall back to centralized HA token for HA MCP servers
            try:
                from glados.core.config_store import cfg
                token = cfg.ha_token
            except Exception:
                pass
        if token:
            headers.setdefault("Authorization", f"Bearer {token}")

        if config.transport == "http":
            import httpx as _httpx
            http_client = _httpx.AsyncClient(headers=headers)
            # streamable_http_client yields (read, write, get_session_id) — discard the 3rd
            async with streamable_http_client(str(config.url), http_client=http_client) as (read_stream, write_stream, _get_session_id):
                yield (read_stream, write_stream)
                return
        if config.transport == "sse":
            async with sse_client(str(config.url), headers=headers) as streams:
                yield streams
                return

        raise MCPError(f"MCP server '{config.name}' has unsupported transport '{config.transport}'.")

    async def _refresh_tools(self, config: MCPServerConfig, session: ClientSession) -> None:
        response = await session.list_tools()
        tools = self._extract_list(response, "tools")
        entries: dict[str, MCPToolEntry] = {}
        for tool in tools:
            tool_name = self._get_field(tool, "name")
            if not tool_name:
                continue
            if not self._tool_allowed(tool_name, config):
                continue
            description = self._get_field(tool, "description")
            input_schema = self._coerce_dict(self._get_field(tool, "inputSchema", "input_schema"))
            entry = MCPToolEntry(
                server=config.name,
                name=tool_name,
                description=description,
                input_schema=input_schema,
            )
            full_name = self._build_tool_name(config.name, tool_name)
            entries[full_name] = entry

        with self._tool_lock:
            self._tool_registry = {
                tool_name: entry
                for tool_name, entry in self._tool_registry.items()
                if entry.server != config.name
            }
            self._tool_registry.update(entries)
        if self._observability_bus:
            self._observability_bus.emit(
                source="mcp",
                kind="tools",
                message=f"{config.name} tools refreshed",
                meta={"count": len(entries)},
            )

    async def _refresh_resources(self, config: MCPServerConfig, session: ClientSession) -> None:
        if not config.context_resources:
            return
        for uri in config.context_resources:
            message = await self._read_resource(session, config.name, uri)
            if message:
                self._cache_resource(config.name, uri, message, config.resource_ttl_s)

    async def _call_tool_async(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        session = self._sessions.get(server_name)
        if not session:
            raise MCPError(f"MCP server '{server_name}' is not connected.")
        result = await session.call_tool(tool_name, arguments)
        error_flag = self._get_field(result, "isError", "is_error")
        content = self._render_contents(self._get_field(result, "content") or [])
        if error_flag:
            raise MCPToolError(content or "MCP tool reported an error.")
        return content or "success"

    async def _read_resource(self, session: ClientSession, server_name: str, uri: str) -> dict[str, str] | None:
        response = await session.read_resource(uri)
        contents = self._extract_list(response, "contents")
        text = self._render_contents(contents)
        if not text:
            return None
        return {
            "role": "system",
            "content": f"[mcp:{server_name}] Resource {uri}\n{text}",
        }

    def _fetch_resource(self, server_name: str, uri: str, timeout: float) -> dict[str, str] | None:
        if self._loop is None:
            return None
        future = asyncio.run_coroutine_threadsafe(self._fetch_resource_async(server_name, uri), self._loop)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError as exc:
            future.cancel()
            logger.warning(f"MCP: resource '{uri}' from {server_name} timed out.")
            return None
        except Exception as exc:
            future.cancel()
            logger.warning(f"MCP: failed to fetch resource '{uri}' from {server_name}: {exc}")
            return None

    async def _fetch_resource_async(self, server_name: str, uri: str) -> dict[str, str] | None:
        session = self._sessions.get(server_name)
        if not session:
            return None
        return await self._read_resource(session, server_name, uri)

    async def _refresh_resource_async(self, server_name: str, uri: str, ttl: float) -> None:
        try:
            message = await self._fetch_resource_async(server_name, uri)
            if message:
                self._cache_resource(server_name, uri, message, ttl)
        except Exception as exc:
            logger.warning(f"MCP: failed to refresh resource '{uri}' from {server_name}: {exc}")
        finally:
            self._mark_resource_refresh_complete(server_name, uri)

    def _schedule_resource_refresh(self, server_name: str, uri: str, ttl: float) -> None:
        if self._loop is None:
            return
        key = (server_name, uri)
        with self._resource_lock:
            if key in self._resource_refreshing:
                return
            self._resource_refreshing.add(key)
        try:
            asyncio.run_coroutine_threadsafe(
                self._refresh_resource_async(server_name, uri, ttl),
                self._loop,
            )
        except RuntimeError as exc:
            logger.warning(f"MCP: failed to schedule refresh for '{uri}' from {server_name}: {exc}")
            self._mark_resource_refresh_complete(server_name, uri)

    def _mark_resource_refresh_complete(self, server_name: str, uri: str) -> None:
        with self._resource_lock:
            self._resource_refreshing.discard((server_name, uri))

    def _cache_resource(self, server_name: str, uri: str, message: dict[str, str], ttl: float) -> None:
        expires_at = time.time() + ttl if ttl > 0 else time.time()
        with self._resource_lock:
            self._resource_cache[(server_name, uri)] = _ResourceCacheEntry(message=message, expires_at=expires_at)

    def _get_cached_resource(
        self,
        server_name: str,
        uri: str,
        allow_expired: bool = False,
    ) -> _ResourceCacheEntry | None:
        with self._resource_lock:
            entry = self._resource_cache.get((server_name, uri))
        if not entry:
            return None
        if allow_expired or entry.expires_at >= time.time():
            return entry
        return None

    def _clear_resource_cache(self, server_name: str) -> None:
        with self._resource_lock:
            keys = [key for key in self._resource_cache if key[0] == server_name]
            for key in keys:
                self._resource_cache.pop(key, None)
            refreshing = {key for key in self._resource_refreshing if key[0] == server_name}
            for key in refreshing:
                self._resource_refreshing.discard(key)

    def _remove_tools_for_server(self, server_name: str) -> None:
        with self._tool_lock:
            self._tool_registry = {
                tool_name: entry
                for tool_name, entry in self._tool_registry.items()
                if entry.server != server_name
            }

    def _tool_entry_to_definition(self, tool_name: str, entry: MCPToolEntry) -> dict[str, Any]:
        schema = entry.input_schema or {"type": "object", "properties": {}}
        description = entry.description or f"MCP tool '{entry.name}' from server '{entry.server}'."
        return {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": description,
                "parameters": schema,
            },
        }

    def _parse_tool_name(self, tool_name: str) -> tuple[str, str]:
        if not tool_name.startswith("mcp."):
            raise MCPError(f"Tool name '{tool_name}' is not an MCP tool.")
        parts = tool_name.split(".")
        if len(parts) < 3:
            raise MCPError(f"Tool name '{tool_name}' is missing server or tool name.")
        server_name = parts[1]
        local_tool = ".".join(parts[2:])
        return server_name, local_tool

    @staticmethod
    def _build_tool_name(server_name: str, tool_name: str) -> str:
        return f"mcp.{server_name}.{tool_name}"

    @staticmethod
    def _tool_allowed(tool_name: str, config: MCPServerConfig) -> bool:
        if config.allowed_tools:
            return any(fnmatch.fnmatch(tool_name, pattern) for pattern in config.allowed_tools)
        if config.blocked_tools:
            return not any(fnmatch.fnmatch(tool_name, pattern) for pattern in config.blocked_tools)
        return True

    @staticmethod
    def _get_field(obj: Any, *fields: str) -> Any:
        if isinstance(obj, dict):
            for field in fields:
                if field in obj:
                    return obj[field]
            return None
        for field in fields:
            if hasattr(obj, field):
                return getattr(obj, field)
        return None

    @staticmethod
    def _extract_list(obj: Any, field: str) -> list[Any]:
        value = MCPManager._get_field(obj, field)
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if hasattr(value, "model_dump"):
            dumped = value.model_dump()
            if isinstance(dumped, list):
                return dumped
        return list(value) if isinstance(value, Iterable) else []

    @staticmethod
    def _coerce_dict(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        if hasattr(value, "model_dump"):
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                return dumped
        if hasattr(value, "dict"):
            dumped = value.dict()
            if isinstance(dumped, dict):
                return dumped
        return None

    @staticmethod
    def _render_contents(contents: Iterable[Any]) -> str:
        parts: list[str] = []
        for item in contents:
            if isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                    continue
                if "data" in item:
                    parts.append(str(item["data"]))
                    continue
            text = MCPManager._get_field(item, "text")
            if text:
                parts.append(str(text))
                continue
            if item is not None:
                parts.append(str(item))
        return "\n".join(part for part in parts if part).strip()
