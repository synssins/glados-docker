"""MCPManager.add_server / remove_server / event ring / log rotation."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from glados.mcp.config import MCPServerConfig
from glados.mcp.manager import MCPError, MCPManager


@asynccontextmanager
async def _fake_transport():
    """A no-op transport for tests — yields two AsyncMock streams."""
    from unittest.mock import AsyncMock
    yield (AsyncMock(), AsyncMock())


def _cfg(name: str = "demo") -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="http", url="https://example.test/mcp")


def _make_manager() -> MCPManager:
    mgr = MCPManager(servers=[])
    # Patch ClientSession + transport so real sessions never open.
    mgr._sessions = {}
    return mgr


def test_add_server_registers_in_servers_and_tasks(monkeypatch):
    mgr = _make_manager()
    # Stub the session runner to be a no-op coro
    started = asyncio.Event()
    async def _fake_runner(cfg):
        started.set()
        await asyncio.sleep(60)  # held open until cancel
    mgr._session_runner = _fake_runner  # type: ignore[assignment]
    mgr.start()
    try:
        cfg = _cfg("demo")
        mgr.add_server(cfg)
        assert "demo" in mgr._servers
        assert "demo" in mgr._session_tasks
    finally:
        mgr.shutdown()


def test_add_server_duplicate_name_raises():
    mgr = _make_manager()
    async def _fake_runner(cfg): await asyncio.sleep(60)
    mgr._session_runner = _fake_runner
    mgr.start()
    try:
        mgr.add_server(_cfg("demo"))
        with pytest.raises(MCPError, match="already"):
            mgr.add_server(_cfg("demo"))
    finally:
        mgr.shutdown()


def test_remove_server_cancels_task_and_drops_from_servers():
    mgr = _make_manager()
    async def _fake_runner(cfg): await asyncio.sleep(60)
    mgr._session_runner = _fake_runner
    mgr.start()
    try:
        mgr.add_server(_cfg("demo"))
        mgr.remove_server("demo")
        assert "demo" not in mgr._servers
        assert "demo" not in mgr._session_tasks
    finally:
        mgr.shutdown()


def test_remove_server_missing_is_noop():
    mgr = _make_manager()
    async def _fake_runner(cfg): await asyncio.sleep(60)
    mgr._session_runner = _fake_runner
    mgr.start()
    try:
        mgr.remove_server("not-there")  # no raise
    finally:
        mgr.shutdown()


def test_event_ring_records_per_plugin():
    mgr = _make_manager()
    mgr._record_event("demo", kind="connect", message="hello")
    mgr._record_event("demo", kind="error", message="oops")
    mgr._record_event("other", kind="connect", message="hi")
    events_demo = mgr.get_plugin_events("demo")
    assert len(events_demo) == 2
    assert events_demo[0]["kind"] == "connect"
    assert events_demo[1]["kind"] == "error"
    events_other = mgr.get_plugin_events("other")
    assert len(events_other) == 1


def test_event_ring_caps_at_256():
    mgr = _make_manager()
    for i in range(300):
        mgr._record_event("demo", kind="connect", message=f"e{i}")
    events = mgr.get_plugin_events("demo", limit=500)
    assert len(events) == 256  # ring cap
    assert events[0]["message"] == "e44"  # oldest = 300 - 256


def test_stdio_log_rotates_when_over_1mb(tmp_path: Path, monkeypatch):
    log_dir = tmp_path / "logs" / "plugins"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "demo.log"
    log_path.write_bytes(b"x" * (1024 * 1024 + 1))  # 1 MB + 1 byte

    from glados.mcp.manager import _rotate_log_if_needed
    _rotate_log_if_needed(log_path)
    assert (log_dir / "demo.log.1").exists()
    assert not log_path.exists() or log_path.stat().st_size == 0
