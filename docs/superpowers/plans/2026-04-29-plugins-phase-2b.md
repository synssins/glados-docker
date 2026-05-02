# Plugins Phase 2b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Phase 2a plugin scaffolding (Change 31) end-to-end usable: stdio plugins spawn via uvx/npx with per-plugin caches; the WebUI exposes install / configure / enable-toggle / logs / browse via a gear-icon-modal UX; everything is gated by `GLADOS_PLUGINS_ENABLED`.

**Architecture:** Five-layer stack — Dockerfile (uvx + Node 20 + log dir) → `glados/plugins/{runner,store}` (cache routing + install/remove helpers) → `glados/mcp/manager` (`add_server` / `remove_server` + per-plugin event ring + log file errlog) → `tts_ui.py` (11 `/api/plugins/*` endpoints) → `ui.js` (panel + gear-modal + browse card). Spec at [`docs/superpowers/specs/2026-04-29-plugins-phase-2b-design.md`](../specs/2026-04-29-plugins-phase-2b-design.md).

**Tech Stack:** Python 3.12, Pydantic v2, MCP client (`mcp` package), httpx for manifest fetch, vanilla JS (no build step) for the WebUI. uv 0.5+ (uvx) and Node 20 (npx) baked into the image. pytest for tests; existing scripts/_local_deploy.py for deploys.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `Dockerfile` | Add uv + Node 20 + `/app/logs/plugins` mkdir | modified |
| `glados/core/engine.py` | Gate `discover_plugins` on `GLADOS_PLUGINS_ENABLED` | modified |
| `glados/plugins/runner.py` | Inject `--cache-dir` (uvx) / `npm_config_cache` env (npx) into stdio configs | modified |
| `glados/plugins/store.py` | `install_plugin`, `remove_plugin`, `set_enabled`, `slugify` | modified |
| `glados/mcp/manager.py` | `add_server`, `remove_server`, per-plugin event ring, per-plugin errlog with rotation | modified |
| `glados/core/config_store.py` | `ServicesConfig.plugin_indexes: list[str]` | modified |
| `glados/webui/tts_ui.py` | 11 `/api/plugins/*` endpoints | modified |
| `glados/webui/static/ui.js` | Panel under System→Services with installed list / Add-by-URL / Browse + gear-modal | modified |
| `tests/test_plugins_runner.py` | New — cache flag/env injection | created |
| `tests/test_plugins_store.py` | New — install/remove/set_enabled/slugify | created |
| `tests/test_mcp_manager_lifecycle.py` | New — add/remove server, event ring, log rotation | created |
| `tests/test_webui_plugins.py` | New — 11 endpoint round-trip + SSRF + browse merge | created |
| `tests/test_engine_plugin_gate.py` | New — `GLADOS_PLUGINS_ENABLED` off skips discovery | created |
| `tests/test_services_config_plugin_indexes.py` | New — round-trip on services.yaml | created |
| `docs/plugins-architecture.md` | Phase 2b status flipped to live | modified |
| `docs/CHANGES.md` | Change 32 entry | modified |
| `README.md` | Plugins section: install + browse flows | modified |

---

## Task 0: Dockerfile + GLADOS_PLUGINS_ENABLED gate

**Goal:** Image ships uvx + Node 20 + plugin log directory. Engine reads `GLADOS_PLUGINS_ENABLED` (default `true`) and skips `discover_plugins` when off.

**Files:**
- Modify: `Dockerfile:20-31`
- Modify: `glados/core/engine.py:700-715` (the existing plugin discover block)
- Create: `tests/test_engine_plugin_gate.py`

**Acceptance Criteria:**
- [ ] `docker run --rm <image> which uvx` returns `/usr/local/bin/uvx` (or wherever pip put it).
- [ ] `docker run --rm <image> which npx` returns `/usr/bin/npx`.
- [ ] `/app/logs/plugins/` exists in the image with operator-writable perms.
- [ ] When `GLADOS_PLUGINS_ENABLED=false`, `discover_plugins` is not called and engine logs `Plugins disabled by GLADOS_PLUGINS_ENABLED env`.
- [ ] When unset (default), behavior matches Phase 2a (discover runs).
- [ ] 3 new tests pass.

**Verify:** `python -m pytest tests/test_engine_plugin_gate.py -v` → 3 passed.

**Steps:**

- [ ] **Step 1: Write the engine gate test (failing)**

```python
# tests/test_engine_plugin_gate.py
"""GLADOS_PLUGINS_ENABLED gate behavior."""
from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest


def test_plugins_enabled_default_true(monkeypatch):
    """Unset env var → discover_plugins is called."""
    monkeypatch.delenv("GLADOS_PLUGINS_ENABLED", raising=False)
    discover_mock = MagicMock(return_value=[])
    with patch("glados.plugins.discover_plugins", discover_mock):
        from glados.core.engine import _maybe_discover_plugin_configs
        _maybe_discover_plugin_configs()
    discover_mock.assert_called_once()


def test_plugins_disabled_env_skips_discovery(monkeypatch, caplog):
    """GLADOS_PLUGINS_ENABLED=false → discover_plugins not called, info log emitted."""
    monkeypatch.setenv("GLADOS_PLUGINS_ENABLED", "false")
    discover_mock = MagicMock(return_value=[])
    with patch("glados.plugins.discover_plugins", discover_mock):
        from glados.core.engine import _maybe_discover_plugin_configs
        configs = _maybe_discover_plugin_configs()
    discover_mock.assert_not_called()
    assert configs == []


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_plugins_enabled_truthy_values(monkeypatch, value):
    """Various truthy strings all enable discovery."""
    monkeypatch.setenv("GLADOS_PLUGINS_ENABLED", value)
    discover_mock = MagicMock(return_value=[])
    with patch("glados.plugins.discover_plugins", discover_mock):
        from glados.core.engine import _maybe_discover_plugin_configs
        _maybe_discover_plugin_configs()
    discover_mock.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_engine_plugin_gate.py -v`
Expected: FAIL — `_maybe_discover_plugin_configs` doesn't exist yet.

- [ ] **Step 3: Refactor engine.py to extract gateable helper**

Locate the existing block in `glados/core/engine.py` around line 700-715 that imports `discover_plugins`. Replace with:

```python
# In glados/core/engine.py, near the existing import block at top:
import os

# Replace the inline plugin discovery block with this helper at module level
# (move it OUT of __init__):
def _maybe_discover_plugin_configs() -> "list[MCPServerConfig]":
    """Discover plugins iff GLADOS_PLUGINS_ENABLED is truthy.

    Returns an empty list when disabled or when the discovery layer
    raises. Never propagates plugin-layer errors to the engine init.
    """
    enabled = os.environ.get("GLADOS_PLUGINS_ENABLED", "true").lower()
    if enabled not in ("1", "true", "yes", "on"):
        logger.info("Plugins disabled by GLADOS_PLUGINS_ENABLED env")
        return []

    plugin_mcp_configs: list[MCPServerConfig] = []
    try:
        from glados.plugins import discover_plugins, plugin_to_mcp_config
        for plugin in discover_plugins():
            try:
                plugin_mcp_configs.append(plugin_to_mcp_config(plugin))
            except Exception as exc:
                logger.warning(
                    "Plugin {!s} failed to materialize MCP config; skipping: {}",
                    plugin.name, exc,
                )
    except Exception as exc:
        logger.warning("Plugin discovery layer failed; skipping: {}", exc)
    return plugin_mcp_configs
```

Then update the call site inside `__init__` to call the helper:

```python
# Was: inline plugin_mcp_configs = []; try: ...
# Now: 
plugin_mcp_configs = _maybe_discover_plugin_configs()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_engine_plugin_gate.py -v`
Expected: PASS — 3 passed.

- [ ] **Step 5: Patch the Dockerfile**

```dockerfile
# Replace the existing system-deps RUN block at line 20:
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Replace the existing pip install line at 27:
RUN pip install --no-cache-dir -e ".[api]" \
    && pip install --no-cache-dir certbot certbot-dns-cloudflare \
    && pip install --no-cache-dir uv

# Add /app/logs/plugins to the existing mkdir at line 31:
RUN mkdir -p /app/configs /app/data /app/logs /app/logs/plugins /app/audio_files /app/certs /app/models
```

- [ ] **Step 6: Verify image build locally is NOT done — operator's CLAUDE.md says no Docker on this host. Skip to deploy.**

The build will run on the docker host via `scripts/_local_deploy.py` (T13). Validate via the deploy step.

- [ ] **Step 7: Run the full suite to confirm nothing else broke**

Run: `python -m pytest -q`
Expected: 1522 passed (1519 + 3 new).

- [ ] **Step 8: Commit**

```bash
cd C:/src/glados-container/.worktrees/webui-polish
git add Dockerfile glados/core/engine.py tests/test_engine_plugin_gate.py
git commit -m "feat(plugins): gate discovery on GLADOS_PLUGINS_ENABLED + ship uvx + node 20

Image: pip-install uv (uvx onto PATH, ~25 MB), apt-install nodejs 20
via NodeSource (npx onto PATH, ~30 MB), mkdir /app/logs/plugins.

Engine: extract plugin discovery into _maybe_discover_plugin_configs
helper that no-ops when GLADOS_PLUGINS_ENABLED is falsy. Default true,
so existing operators see no behavior change. False neutralizes the
runtime entirely."
```

---

## Task 1: Runner cache injection (uvx + npx)

**Goal:** `runner.py` injects `--cache-dir <plugin_dir>/.uvx-cache` for uvx packages and `npm_config_cache=<plugin_dir>/.uvx-cache` env for npx packages, so plugin caches survive image rebuilds.

**Files:**
- Modify: `glados/plugins/runner.py:128-148, 174-189`
- Create: `tests/test_plugins_runner.py`

**Acceptance Criteria:**
- [ ] uvx plugin → `args` starts with `[<pkg>@<ver>, --cache-dir, <plugin>/.uvx-cache, ...]`.
- [ ] npx plugin → `env["npm_config_cache"] == "<plugin>/.uvx-cache"`.
- [ ] dnx plugin (deferred) → no cache injection (acceptable; not used in v1).
- [ ] 4 new tests pass; existing `test_plugins_loader.py` still passes.

**Verify:** `python -m pytest tests/test_plugins_runner.py tests/test_plugins_loader.py -v`

**Steps:**

- [ ] **Step 1: Write tests (failing)**

```python
# tests/test_plugins_runner.py
"""Runner cache injection: uvx via --cache-dir flag, npx via env."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from glados.plugins.loader import load_plugin
from glados.plugins.runner import plugin_to_mcp_config


def _write_plugin(tmp_path: Path, slug: str, manifest: dict, runtime: dict) -> Path:
    plugin_dir = tmp_path / slug
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(__import__("json").dumps(manifest))
    (plugin_dir / "runtime.yaml").write_text(yaml.safe_dump(runtime))
    return plugin_dir


def _uvx_manifest(name: str = "demo.python") -> dict:
    return {
        "name": name,
        "description": "demo",
        "version": "0.1.0",
        "packages": [{
            "registryType": "pypi",
            "identifier": "demo-mcp",
            "version": "1.2.3",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
        }],
    }


def _npx_manifest(name: str = "demo.node") -> dict:
    return {
        "name": name,
        "description": "demo",
        "version": "0.1.0",
        "packages": [{
            "registryType": "npm",
            "identifier": "@demo/mcp",
            "version": "1.2.3",
            "runtimeHint": "npx",
            "transport": {"type": "stdio"},
        }],
    }


def test_uvx_injects_cache_dir_flag(tmp_path: Path):
    plugin_dir = _write_plugin(
        tmp_path, "demo",
        _uvx_manifest(),
        {"plugin": "demo.python", "package_index": 0},
    )
    plugin = load_plugin(plugin_dir)
    cfg = plugin_to_mcp_config(plugin)

    assert cfg.transport == "stdio"
    assert cfg.command == "uvx"
    assert cfg.args[0] == "demo-mcp@1.2.3"
    assert "--cache-dir" in cfg.args
    cache_idx = cfg.args.index("--cache-dir")
    assert cfg.args[cache_idx + 1] == str(plugin_dir / ".uvx-cache")


def test_npx_injects_npm_config_cache_env(tmp_path: Path):
    plugin_dir = _write_plugin(
        tmp_path, "demo",
        _npx_manifest(),
        {"plugin": "demo.node", "package_index": 0},
    )
    plugin = load_plugin(plugin_dir)
    cfg = plugin_to_mcp_config(plugin)

    assert cfg.transport == "stdio"
    assert cfg.command == "npx"
    assert cfg.args[0] == "@demo/mcp@1.2.3"
    assert "--cache-dir" not in cfg.args  # npx uses env, not flag
    assert cfg.env is not None
    assert cfg.env["npm_config_cache"] == str(plugin_dir / ".uvx-cache")


def test_uvx_cache_dir_appears_after_package_identifier(tmp_path: Path):
    """--cache-dir must come after the package@version arg so uvx parses it correctly."""
    plugin_dir = _write_plugin(
        tmp_path, "demo",
        _uvx_manifest(),
        {"plugin": "demo.python", "package_index": 0},
    )
    plugin = load_plugin(plugin_dir)
    cfg = plugin_to_mcp_config(plugin)
    pkg_idx = cfg.args.index("demo-mcp@1.2.3")
    cache_idx = cfg.args.index("--cache-dir")
    assert cache_idx > pkg_idx


def test_remote_plugin_unaffected(tmp_path: Path):
    """Remote plugins don't grow a cache flag."""
    manifest = {
        "name": "demo.remote",
        "description": "demo",
        "version": "0.1.0",
        "remotes": [{"type": "streamable-http", "url": "https://example.test/mcp"}],
    }
    plugin_dir = _write_plugin(
        tmp_path, "demo-remote",
        manifest,
        {"plugin": "demo.remote", "remote_index": 0},
    )
    plugin = load_plugin(plugin_dir)
    cfg = plugin_to_mcp_config(plugin)
    assert cfg.transport == "http"
    assert cfg.args == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_plugins_runner.py -v`
Expected: FAIL — cache flag not injected.

- [ ] **Step 3: Patch `runner.py:_build_stdio_args`**

```python
# In glados/plugins/runner.py, replace _build_stdio_args around line 128:
def _build_stdio_args(package: Package, plugin: Plugin) -> list[str]:
    """Build the argv list for an stdio plugin.

    For uvx: [<pkg>@<ver>, --cache-dir, <plugin>/.uvx-cache, ...packageArguments]
    For npx: [<pkg>@<ver>, ...packageArguments]  (cache via env, see _resolve_env)
    """
    args: list[str] = [f"{package.identifier}@{package.version}"]

    # Per-plugin cache routing. Phase 2b: uvx accepts --cache-dir as a CLI flag.
    # npx ignores it and uses the npm_config_cache env var instead (see _resolve_env).
    if package.runtime_hint == "uvx":
        cache_dir = plugin.directory / ".uvx-cache"
        args.extend(["--cache-dir", str(cache_dir)])

    for arg in package.package_arguments:
        rendered = _render_argument(arg, plugin)
        if rendered:
            args.extend(rendered)

    return args
```

- [ ] **Step 4: Patch `runner.py:_resolve_env` to inject `npm_config_cache` for npx**

```python
# In glados/plugins/runner.py, append AT THE END of _resolve_env after the existing for-loop:
def _resolve_env(package: Package, plugin: Plugin) -> dict[str, str]:
    """Merge runtime.yaml.env_values + secrets.env, applying defaults
    from server.json for any unset env. Raise on missing required envs."""
    env: dict[str, str] = {}
    for ev in package.environment_variables:
        value = plugin.secrets.get(ev.name) or plugin.runtime.env_values.get(ev.name)
        if value is None and ev.default is not None:
            value = ev.default
        if value is None and ev.is_required:
            raise ManifestError(
                f"plugin {plugin.name} requires env {ev.name!r} (set it in "
                f"runtime.yaml.env_values or secrets.env)"
            )
        if value is not None:
            env[ev.name] = value

    # Phase 2b: npx honors npm_config_cache to redirect its cache dir.
    # uvx uses --cache-dir CLI flag instead (see _build_stdio_args).
    if package.runtime_hint == "npx":
        env["npm_config_cache"] = str(plugin.directory / ".uvx-cache")

    return env
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_plugins_runner.py tests/test_plugins_loader.py -v`
Expected: 4 new pass + existing loader tests pass.

- [ ] **Step 6: Commit**

```bash
git add glados/plugins/runner.py tests/test_plugins_runner.py
git commit -m "feat(plugins): inject per-plugin cache dir for uvx + npx

uvx: append --cache-dir <plugin>/.uvx-cache to args after the
package@version token. npx: set npm_config_cache=<plugin>/.uvx-cache
in env (npx ignores --cache-dir).

Caches under /app/data/plugins/<name>/.uvx-cache/ survive image
rebuilds because /app/data is the persistent volume."
```

---

## Task 2: MCPManager add_server / remove_server / event ring / per-plugin log

**Goal:** Manager supports per-plugin task lifecycle: add a single server config and start its session, remove a single server and cancel its task, route stdio stderr to a per-plugin log file with simple rotation, and keep an in-memory ring of connect/disconnect/tool/error events per server.

**Files:**
- Modify: `glados/mcp/manager.py:62-76, 187-244, 199-233`
- Create: `tests/test_mcp_manager_lifecycle.py`

**Acceptance Criteria:**
- [ ] `MCPManager.add_server(cfg)` schedules `_session_runner` for `cfg`, registers in `_servers` and `_session_tasks`. Raises `MCPError` if a server with that name already exists.
- [ ] `MCPManager.remove_server(name)` cancels the task, awaits up to 5 s, drops from `_servers`. No-op if missing. Logs at warning if cancel-await times out.
- [ ] `MCPManager.get_plugin_events(name, limit=200)` returns up to `limit` recent events for the named server, oldest-first.
- [ ] Event ring is `deque(maxlen=256)` per server.
- [ ] Stdio sessions write stderr to `/app/logs/plugins/<name>.log`. File grown >1 MB is renamed to `<name>.log.1` (replacing prior backup) on next session start.
- [ ] 6 new tests pass.

**Verify:** `python -m pytest tests/test_mcp_manager_lifecycle.py -v`

**Steps:**

- [ ] **Step 1: Write the lifecycle tests (failing)**

```python
# tests/test_mcp_manager_lifecycle.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_manager_lifecycle.py -v`
Expected: FAIL — methods don't exist.

- [ ] **Step 3: Patch `manager.py` — add ring buffer + helpers**

Add to imports at the top of `glados/mcp/manager.py`:

```python
from collections import defaultdict, deque
import os
from pathlib import Path
```

Add to `MCPManager.__init__` after the existing `self._sessions = {}` line:

```python
        # Phase 2b: per-plugin event ring (connect/disconnect/error/tools).
        self._plugin_events: dict[str, deque[dict]] = defaultdict(
            lambda: deque(maxlen=256)
        )
        self._plugin_events_lock = threading.Lock()

        # Phase 2b: log dir for stdio plugin stderr.
        self._plugin_log_dir = Path(
            os.environ.get("GLADOS_PLUGIN_LOG_DIR", "/app/logs/plugins")
        )
```

Add `_record_event` and `get_plugin_events` methods to the class:

```python
    def _record_event(self, server_name: str, *, kind: str, message: str,
                      level: str = "info", meta: dict | None = None) -> None:
        """Append an event to the per-plugin ring. Also bridges to the
        ObservabilityBus when one is configured."""
        import time as _time
        entry = {
            "ts": _time.time(),
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
```

- [ ] **Step 4: Add the log-rotation helper at module level**

```python
# Add at module level in manager.py, near other helpers:
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
```

- [ ] **Step 5: Replace existing `_session_runner` to bridge events + use per-plugin log**

Update the existing `_session_runner` in `manager.py` (around line 191-233). Replace the connect/disconnect/error emit calls with `self._record_event(config.name, ...)`:

```python
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
                if hasattr(exc, "exceptions"):
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
```

- [ ] **Step 6: Patch `_open_transport` to route stdio stderr to per-plugin log**

In `_open_transport`, replace the stdio branch (around line 237-243):

```python
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
```

- [ ] **Step 7: Add `add_server` and `remove_server` public methods**

Append to the `MCPManager` class (somewhere near `start`/`shutdown` methods):

```python
    def add_server(self, config: MCPServerConfig) -> None:
        """Thread-safe. Register a new server and schedule its session
        runner on the manager's loop. Raises if a server with the same
        name is already registered."""
        if self._loop is None:
            raise MCPError("MCP manager is not running.")
        if config.name in self._servers:
            raise MCPError(f"MCP server '{config.name}' is already registered.")

        self._servers[config.name] = config

        def _spawn() -> None:
            assert self._loop is not None
            task = self._loop.create_task(self._session_runner(config))
            self._session_tasks[config.name] = task

        self._loop.call_soon_threadsafe(_spawn)

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
            except (FuturesTimeoutError, Exception) as exc:
                logger.warning(
                    "MCP: remove_server('{!s}') cleanup did not complete in {} s: {}",
                    name, timeout, exc,
                )

        self._sessions.pop(name, None)
        self._session_tasks.pop(name, None)
        self._servers.pop(name, None)
        self._remove_tools_for_server(name)
        self._clear_resource_cache(name)

    @staticmethod
    async def _await_task(task: asyncio.Task) -> None:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_manager_lifecycle.py -v`
Expected: 6 passed.

- [ ] **Step 9: Run the full suite**

Run: `python -m pytest -q`
Expected: still green (1528 passed: 1519 + 3 from T0 + 6 new).

- [ ] **Step 10: Commit**

```bash
git add glados/mcp/manager.py tests/test_mcp_manager_lifecycle.py
git commit -m "feat(mcp): add_server/remove_server + per-plugin event ring + stderr log rotation

MCPManager grows three Phase 2b extensions:

1. Per-plugin lifecycle: add_server(cfg) schedules _session_runner on
   the manager's loop and registers in _servers/_session_tasks.
   remove_server(name) cancels the task, awaits up to 5 s, drops from
   internal state. Toggling one plugin no longer disconnects others.

2. In-memory event ring per server (deque maxlen=256). connect /
   disconnect / error / tools events recorded via _record_event;
   surfaced to the WebUI in T4 via get_plugin_events.

3. stdio errlog routes to /app/logs/plugins/<name>.log instead of
   DEVNULL. Lazy size-cap rotation: file >1 MB is renamed to .log.1
   on next session start. One backup retained, ~2 MB ceiling per
   plugin. Falls back to DEVNULL if the log dir isn't writable."
```

---

## Task 3: Plugin store install / remove / set_enabled / slugify

**Goal:** `store.py` grows the helpers the WebUI install/remove/enable handlers need: atomic write of a fresh plugin directory, recursive delete, enable-flag flip, and slug-from-name with collision suffix.

**Files:**
- Modify: `glados/plugins/store.py` (append four new functions)
- Modify: `glados/plugins/__init__.py` (export the new helpers)
- Create: `tests/test_plugins_store.py`

**Acceptance Criteria:**
- [ ] `slugify(name, existing)` returns lowercased last-segment with non-alphanumeric → `-`. Collision adds `-2`, `-3`, ... up to 100 (raises after).
- [ ] `install_plugin(plugins_dir, slug, manifest)` writes `<slug>/server.json` + stub `runtime.yaml` (`enabled: false`, `package_index` or `remote_index` set to 0). Atomic via `<slug>.installing/` rename. Refuses if `<slug>/` exists.
- [ ] `remove_plugin(plugins_dir, slug)` rmtree-s the directory. Refuses paths outside `plugins_dir` (basic safety).
- [ ] `set_enabled(plugin_dir, enabled)` flips `runtime.yaml.enabled`, returns the new `RuntimeConfig`.
- [ ] 8 new tests pass.

**Verify:** `python -m pytest tests/test_plugins_store.py -v`

**Steps:**

- [ ] **Step 1: Write tests (failing)**

```python
# tests/test_plugins_store.py
"""install_plugin / remove_plugin / set_enabled / slugify."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from glados.plugins.errors import InstallError
from glados.plugins.manifest import ServerJSON
from glados.plugins.store import (
    install_plugin,
    load_runtime,
    remove_plugin,
    set_enabled,
    slugify,
)


def test_slugify_simple_name():
    assert slugify("mcp-arr", set()) == "mcp-arr"
    assert slugify("MCP-ARR", set()) == "mcp-arr"


def test_slugify_reverse_dns_takes_last_segment():
    assert slugify("io.github.aplaceforallmystuff/mcp-arr", set()) == "mcp-arr"


def test_slugify_collision_appends_suffix():
    existing = {"mcp-arr"}
    assert slugify("mcp-arr", existing) == "mcp-arr-2"
    existing.add("mcp-arr-2")
    assert slugify("mcp-arr", existing) == "mcp-arr-3"


def test_slugify_strips_non_alphanumeric():
    assert slugify("foo bar!@#baz", set()) == "foo-bar-baz"


def _manifest(name: str = "demo.python", local: bool = True) -> ServerJSON:
    raw = {
        "name": name,
        "description": "demo",
        "version": "0.1.0",
    }
    if local:
        raw["packages"] = [{
            "registryType": "pypi",
            "identifier": "demo-mcp",
            "version": "1.0.0",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
        }]
    else:
        raw["remotes"] = [{"type": "streamable-http", "url": "https://x.test/mcp"}]
    return ServerJSON.model_validate(raw)


def test_install_plugin_creates_directory_with_files(tmp_path: Path):
    install_plugin(tmp_path, "demo", _manifest())
    assert (tmp_path / "demo" / "server.json").exists()
    assert (tmp_path / "demo" / "runtime.yaml").exists()


def test_install_plugin_stub_runtime_disabled_and_correct_index(tmp_path: Path):
    install_plugin(tmp_path, "demo-local", _manifest(local=True))
    rt = load_runtime(tmp_path / "demo-local")
    assert rt.enabled is False
    assert rt.package_index == 0
    assert rt.remote_index is None

    install_plugin(tmp_path, "demo-remote", _manifest("demo.remote", local=False))
    rt = load_runtime(tmp_path / "demo-remote")
    assert rt.enabled is False
    assert rt.remote_index == 0
    assert rt.package_index is None


def test_install_plugin_refuses_existing_dir(tmp_path: Path):
    install_plugin(tmp_path, "demo", _manifest())
    with pytest.raises(InstallError, match="already exists"):
        install_plugin(tmp_path, "demo", _manifest())


def test_remove_plugin_rmtree(tmp_path: Path):
    install_plugin(tmp_path, "demo", _manifest())
    assert (tmp_path / "demo").exists()
    remove_plugin(tmp_path, "demo")
    assert not (tmp_path / "demo").exists()


def test_remove_plugin_missing_is_noop(tmp_path: Path):
    remove_plugin(tmp_path, "not-there")  # no raise


def test_remove_plugin_refuses_path_outside_plugins_dir(tmp_path: Path):
    with pytest.raises(InstallError, match="outside"):
        remove_plugin(tmp_path, "../escape")


def test_set_enabled_round_trip(tmp_path: Path):
    install_plugin(tmp_path, "demo", _manifest())
    plugin_dir = tmp_path / "demo"

    rt = set_enabled(plugin_dir, True)
    assert rt.enabled is True
    rt2 = load_runtime(plugin_dir)
    assert rt2.enabled is True

    rt = set_enabled(plugin_dir, False)
    assert rt.enabled is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_plugins_store.py -v`
Expected: FAIL — helpers don't exist.

- [ ] **Step 3: Append helpers to `store.py`**

```python
# Append to glados/plugins/store.py:

import json
import re
import shutil

from .errors import InstallError


_SLUG_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_SLUG_VALID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def slugify(name: str, existing: set[str]) -> str:
    """Lowercased last path segment with non-alphanumeric → '-'.
    Collisions append '-2', '-3', ... up to '-100' before raising."""
    last = name.rsplit("/", 1)[-1].lower()
    base = _SLUG_NORMALIZE_RE.sub("-", last).strip("-")
    if not base:
        raise InstallError(f"name {name!r} produces an empty slug")
    if base not in existing:
        return base
    for i in range(2, 101):
        candidate = f"{base}-{i}"
        if candidate not in existing:
            return candidate
    raise InstallError(f"slug {base!r} has 100+ collisions; bailing out")


def install_plugin(plugins_dir: Path, slug: str, manifest: "ServerJSON") -> Path:
    """Create plugins_dir/<slug>/ with server.json + a disabled-stub
    runtime.yaml. Atomic via <slug>.installing/ → <slug>/ rename.
    Raises InstallError if <slug>/ already exists or slug is invalid."""
    if not _SLUG_VALID_RE.match(slug):
        raise InstallError(
            f"slug {slug!r} invalid; must match {_SLUG_VALID_RE.pattern}"
        )
    final = plugins_dir / slug
    if final.exists():
        raise InstallError(f"plugin directory {final!s} already exists")

    plugins_dir.mkdir(parents=True, exist_ok=True)
    staging = plugins_dir / f"{slug}.installing"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()

    # server.json — write whatever the operator fetched, verbatim,
    # via the model's JSON dump.
    (staging / "server.json").write_text(
        manifest.model_dump_json(by_alias=True, exclude_none=True, indent=2),
        encoding="utf-8",
    )

    # Stub runtime.yaml. Pick package_index or remote_index based on
    # which the manifest exposes. enabled: false so install never
    # auto-spawns; operator must toggle ON in the WebUI.
    if manifest.packages:
        runtime = RuntimeConfig(
            plugin=manifest.name,
            enabled=False,
            package_index=0,
        )
    elif manifest.remotes:
        runtime = RuntimeConfig(
            plugin=manifest.name,
            enabled=False,
            remote_index=0,
        )
    else:
        shutil.rmtree(staging)
        raise InstallError(
            f"manifest for {manifest.name!r} has neither packages nor remotes"
        )
    save_runtime(staging, runtime)

    staging.rename(final)
    return final


def remove_plugin(plugins_dir: Path, slug: str) -> None:
    """rmtree of plugins_dir/<slug>/. No-op if missing. Refuses paths
    outside plugins_dir (basic .. safety)."""
    target = (plugins_dir / slug).resolve()
    parent = plugins_dir.resolve()
    if parent not in target.parents and target != parent:
        raise InstallError(f"refusing to remove path outside plugins_dir: {target}")
    if target == parent:
        raise InstallError(f"refusing to remove plugins_dir itself: {target}")
    if not target.exists():
        return
    shutil.rmtree(target)


def set_enabled(plugin_dir: Path, enabled: bool) -> RuntimeConfig:
    """Flip runtime.yaml.enabled, save, return the new RuntimeConfig."""
    rt = load_runtime(plugin_dir)
    new_rt = rt.model_copy(update={"enabled": enabled})
    save_runtime(plugin_dir, new_rt)
    return new_rt
```

- [ ] **Step 4: Re-export from `__init__.py`**

```python
# In glados/plugins/__init__.py, add to the imports:
from .store import install_plugin, remove_plugin, set_enabled, slugify

# Append to __all__:
    "install_plugin",
    "remove_plugin",
    "set_enabled",
    "slugify",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_plugins_store.py -v`
Expected: 8 passed.

- [ ] **Step 6: Run full suite**

Run: `python -m pytest -q`
Expected: 1536 passed.

- [ ] **Step 7: Commit**

```bash
git add glados/plugins/store.py glados/plugins/__init__.py tests/test_plugins_store.py
git commit -m "feat(plugins): install_plugin / remove_plugin / set_enabled / slugify

Atomic install via <slug>.installing/ → <slug>/ rename. Stub runtime.yaml
written disabled (enabled: false) so installation never auto-spawns —
operator must toggle ON in the WebUI explicitly.

slugify: lowercased last path segment, non-alphanumeric → '-',
collision suffix '-2'..'-100' before bailing.

remove_plugin refuses paths outside plugins_dir (.. safety).
set_enabled is a thin runtime.yaml flip used by the enable/disable
endpoints in T4."
```

---

## Task 4: Core 8 `/api/plugins/*` endpoints

**Goal:** WebUI can list plugins, get one's full state, install by URL, save runtime config, enable/disable (hot-rotate via MCPManager), delete, and read per-plugin logs.

**Files:**
- Modify: `glados/webui/tts_ui.py:1989-2050` (POST dispatcher), and `do_GET` near 1880 to recognize new GET paths
- Modify: `glados/webui/tts_ui.py` (add 8 handler methods at end of class)
- Create: `tests/test_webui_plugins.py`

**Acceptance Criteria:**
- [ ] All 8 endpoints reachable, admin-only.
- [ ] `GET /api/plugins` returns `{plugins: [...], enabled_globally: bool}`.
- [ ] `POST /api/plugins/install` validates `https://` only, manifest size ≤ 256 KB, fetch timeout 5 s, rejects RFC1918/loopback resolution.
- [ ] `POST /api/plugins/<slug>/enable` calls `MCPManager.add_server(plugin_to_mcp_config(plugin))`.
- [ ] `POST /api/plugins/<slug>/disable` calls `MCPManager.remove_server(slug)`.
- [ ] `DELETE /api/plugins/<slug>` removes any active session and rmtrees the dir.
- [ ] `GET /api/plugins/<slug>/logs` returns `{stdio_log: [...], events: [...]}`.
- [ ] Saving runtime config preserves unchanged secrets when client posts `"***"`.
- [ ] 12 new tests pass.

**Verify:** `python -m pytest tests/test_webui_plugins.py -v`

**Steps:**

- [ ] **Step 1: Write the endpoint tests (failing)**

Test code is long; key cases below. Full file at the path:

```python
# tests/test_webui_plugins.py
"""WebUI /api/plugins/* endpoints — round-trip + auth + SSRF + secrets."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest

# Import the existing test harness fixtures used by other webui tests.
# The harness exposes a `client` fixture that returns an authenticated
# admin HTTP client with cookies set.
pytestmark = pytest.mark.usefixtures("authenticated_admin_client")


def _good_manifest_json(name: str = "demo.python") -> str:
    return json.dumps({
        "name": name,
        "description": "demo plugin",
        "version": "0.1.0",
        "packages": [{
            "registryType": "pypi",
            "identifier": "demo-mcp",
            "version": "1.0.0",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
            "environmentVariables": [
                {"name": "DEMO_KEY", "isSecret": True, "isRequired": True},
                {"name": "DEMO_URL", "isRequired": False, "default": "https://x.test"},
            ],
        }],
    })


def test_list_when_disabled_globally(authenticated_admin_client, monkeypatch):
    monkeypatch.setenv("GLADOS_PLUGINS_ENABLED", "false")
    r = authenticated_admin_client.get("/api/plugins")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled_globally"] is False
    assert data["plugins"] == []


def test_install_https_only(authenticated_admin_client):
    r = authenticated_admin_client.post(
        "/api/plugins/install",
        json={"url": "http://example.test/server.json"},
    )
    assert r.status_code == 400
    assert "https" in r.json()["error"].lower()


def test_install_rejects_loopback(authenticated_admin_client):
    r = authenticated_admin_client.post(
        "/api/plugins/install",
        json={"url": "https://127.0.0.1/server.json"},
    )
    assert r.status_code == 400
    assert "loopback" in r.json()["error"].lower() or "private" in r.json()["error"].lower()


def test_install_rejects_rfc1918(authenticated_admin_client):
    r = authenticated_admin_client.post(
        "/api/plugins/install",
        json={"url": "https://10.0.0.5/server.json"},
    )
    assert r.status_code == 400


def test_install_oversize_response_rejected(authenticated_admin_client, monkeypatch):
    big = "x" * (256 * 1024 + 1)
    fake_resp = MagicMock(status_code=200, text=big, headers={"content-length": str(len(big))})
    fake_resp.iter_bytes = lambda: iter([big.encode()])
    with patch("httpx.get", return_value=fake_resp):
        r = authenticated_admin_client.post(
            "/api/plugins/install",
            json={"url": "https://example.test/server.json"},
        )
    assert r.status_code == 400


def test_install_happy_path_writes_disabled_stub(
    authenticated_admin_client, tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    fake_resp = MagicMock(
        status_code=200, text=_good_manifest_json(), headers={"content-length": "300"},
    )
    with patch("httpx.get", return_value=fake_resp), \
         patch("glados.webui.plugin_endpoints._resolve_safe_host", return_value=True):
        r = authenticated_admin_client.post(
            "/api/plugins/install",
            json={"url": "https://example.test/server.json"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "demo-python"  # slugified
    assert (tmp_path / "demo-python" / "server.json").exists()
    rt = json.loads((tmp_path / "demo-python" / "runtime.yaml").read_text())  # via yaml->json caveat OK
    # runtime.yaml is YAML not JSON; just verify file exists. Detailed
    # round-trip is in test_plugins_store.py.


def test_save_runtime_preserves_unchanged_secrets(
    authenticated_admin_client, tmp_path: Path, monkeypatch,
):
    """When client posts '***' for a secret, server reads existing
    secrets.env and keeps the prior value (no clobber)."""
    # Pre-seed a plugin.
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(_good_manifest_json())
    (plugin_dir / "runtime.yaml").write_text(
        "plugin: demo.python\nenabled: false\npackage_index: 0\n"
    )
    (plugin_dir / "secrets.env").write_text("DEMO_KEY=existing-secret\n")

    r = authenticated_admin_client.post(
        "/api/plugins/demo",
        json={
            "env_values": {"DEMO_URL": "https://changed.test"},
            "secrets": {"DEMO_KEY": "***"},
        },
    )
    assert r.status_code == 200, r.text
    secrets_after = (plugin_dir / "secrets.env").read_text()
    assert "DEMO_KEY=existing-secret" in secrets_after


def test_save_runtime_overwrites_changed_secret(
    authenticated_admin_client, tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(_good_manifest_json())
    (plugin_dir / "runtime.yaml").write_text(
        "plugin: demo.python\nenabled: false\npackage_index: 0\n"
    )
    (plugin_dir / "secrets.env").write_text("DEMO_KEY=existing-secret\n")

    r = authenticated_admin_client.post(
        "/api/plugins/demo",
        json={"env_values": {}, "secrets": {"DEMO_KEY": "new-secret"}},
    )
    assert r.status_code == 200
    secrets_after = (plugin_dir / "secrets.env").read_text()
    assert "DEMO_KEY=new-secret" in secrets_after
    assert "existing-secret" not in secrets_after


def test_enable_calls_add_server(authenticated_admin_client, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(json.dumps({
        "name": "demo.python", "description": "x", "version": "0.1.0",
        "remotes": [{"type": "streamable-http", "url": "https://x.test/mcp"}],
    }))
    (plugin_dir / "runtime.yaml").write_text(
        "plugin: demo.python\nenabled: false\nremote_index: 0\n"
    )

    add_mock = MagicMock()
    with patch("glados.webui.tts_ui._mcp_manager", MagicMock(add_server=add_mock)):
        r = authenticated_admin_client.post("/api/plugins/demo/enable")
    assert r.status_code == 200
    add_mock.assert_called_once()


def test_disable_calls_remove_server(authenticated_admin_client, tmp_path, monkeypatch):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(json.dumps({
        "name": "demo.python", "description": "x", "version": "0.1.0",
        "remotes": [{"type": "streamable-http", "url": "https://x.test/mcp"}],
    }))
    (plugin_dir / "runtime.yaml").write_text(
        "plugin: demo.python\nenabled: true\nremote_index: 0\n"
    )

    remove_mock = MagicMock()
    with patch("glados.webui.tts_ui._mcp_manager", MagicMock(remove_server=remove_mock)):
        r = authenticated_admin_client.post("/api/plugins/demo/disable")
    assert r.status_code == 200
    remove_mock.assert_called_once_with("demo")


def test_delete_removes_session_and_dir(
    authenticated_admin_client, tmp_path, monkeypatch,
):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(json.dumps({
        "name": "demo.python", "description": "x", "version": "0.1.0",
        "remotes": [{"type": "streamable-http", "url": "https://x.test/mcp"}],
    }))
    (plugin_dir / "runtime.yaml").write_text(
        "plugin: demo.python\nenabled: true\nremote_index: 0\n"
    )

    remove_mock = MagicMock()
    with patch("glados.webui.tts_ui._mcp_manager", MagicMock(remove_server=remove_mock)):
        r = authenticated_admin_client.delete("/api/plugins/demo")
    assert r.status_code == 200
    remove_mock.assert_called_once_with("demo")
    assert not plugin_dir.exists()


def test_logs_returns_stdio_tail_and_events(
    authenticated_admin_client, tmp_path, monkeypatch,
):
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(tmp_path))
    monkeypatch.setenv("GLADOS_PLUGIN_LOG_DIR", str(tmp_path / "logs"))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(json.dumps({
        "name": "demo.python", "description": "x", "version": "0.1.0",
        "packages": [{
            "registryType": "pypi", "identifier": "demo", "version": "1.0",
            "runtimeHint": "uvx", "transport": {"type": "stdio"},
        }],
    }))
    (plugin_dir / "runtime.yaml").write_text(
        "plugin: demo.python\nenabled: true\npackage_index: 0\n"
    )
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "demo.log").write_text("startup line\nerror line\n")

    events_mock = MagicMock(return_value=[{"ts": 1, "kind": "connect", "message": "hi"}])
    with patch("glados.webui.tts_ui._mcp_manager",
               MagicMock(get_plugin_events=events_mock)):
        r = authenticated_admin_client.get("/api/plugins/demo/logs?lines=200")
    assert r.status_code == 200
    body = r.json()
    assert "startup line" in body["stdio_log"][0]
    assert body["events"][0]["kind"] == "connect"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_webui_plugins.py -v`
Expected: FAIL — endpoints don't exist.

- [ ] **Step 3: Add endpoint module `glados/webui/plugin_endpoints.py`** (helpers used by handlers)

```python
# glados/webui/plugin_endpoints.py
"""Helpers for the /api/plugins/* HTTP surface in tts_ui.py.

Kept in a separate module so tts_ui.py doesn't grow further. Handlers
in tts_ui.py call into this module; this module contains the URL
fetching / SSRF guard / secret-merge logic that's worth unit-testing
without spinning up the full HTTP server.
"""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from glados.plugins import (
    discover_plugins,
    install_plugin,
    load_plugin,
    plugin_to_mcp_config,
    remove_plugin,
    set_enabled,
    slugify,
)
from glados.plugins.errors import InstallError, ManifestError
from glados.plugins.loader import default_plugins_dir
from glados.plugins.manifest import ServerJSON
from glados.plugins.store import (
    load_runtime,
    load_secrets,
    save_runtime,
    save_secrets,
)


MAX_MANIFEST_BYTES = 256 * 1024
FETCH_TIMEOUT_S = 5.0
SECRET_PLACEHOLDER = "***"


def _resolve_safe_host(host: str) -> bool:
    """True if host resolves to a public IP (rejects loopback / private /
    link-local / multicast). Conservative — refuses on resolution
    failure too."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr_str = info[4][0]
        try:
            addr = ipaddress.ip_address(addr_str)
        except ValueError:
            return False
        if (addr.is_loopback or addr.is_private or
                addr.is_link_local or addr.is_multicast or
                addr.is_reserved):
            return False
    return True


def fetch_manifest(url: str) -> ServerJSON:
    """Fetch a server.json from `url` with all the install-flow guards.
    Raises InstallError with a user-facing message on any failure."""
    if not url.lower().startswith("https://"):
        raise InstallError("URL must use https://")
    parsed = urlparse(url)
    if not parsed.hostname:
        raise InstallError("URL has no host")
    if not _resolve_safe_host(parsed.hostname):
        raise InstallError(
            "URL host resolves to a loopback / private / link-local "
            "address; refusing for SSRF safety"
        )
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True) as client:
            r = client.get(url)
    except httpx.HTTPError as exc:
        raise InstallError(f"manifest fetch failed: {exc}") from exc
    if r.status_code != 200:
        raise InstallError(f"manifest fetch returned HTTP {r.status_code}")
    if len(r.content) > MAX_MANIFEST_BYTES:
        raise InstallError(
            f"manifest too large ({len(r.content)} bytes; max {MAX_MANIFEST_BYTES})"
        )
    try:
        import json as _json
        raw = _json.loads(r.text)
    except Exception as exc:
        raise InstallError(f"manifest is not valid JSON: {exc}") from exc
    try:
        return ServerJSON.model_validate(raw)
    except Exception as exc:
        # Cap pydantic error msg at 1 KB so a malicious manifest can't
        # blow up the response.
        msg = str(exc)[:1024]
        raise InstallError(f"manifest failed schema validation: {msg}") from exc


def list_installed_slugs(plugins_dir: Path) -> set[str]:
    if not plugins_dir.exists():
        return set()
    return {
        d.name for d in plugins_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    }


def install_from_url(url: str, slug_hint: str | None = None) -> dict:
    """Full install-by-URL flow. Returns {slug, manifest_dict}."""
    manifest = fetch_manifest(url)
    plugins_dir = default_plugins_dir()
    existing = list_installed_slugs(plugins_dir)
    slug = slug_hint or slugify(manifest.name, existing)
    if slug in existing:
        # Operator-supplied slug collision: surface the conflict
        # explicitly rather than silently appending -2.
        raise InstallError(f"slug {slug!r} is already installed")
    install_plugin(plugins_dir, slug, manifest)
    return {
        "slug": slug,
        "manifest": manifest.model_dump(by_alias=True, exclude_none=True, mode="json"),
    }


def merge_runtime_save(
    plugin_dir: Path,
    env_values: dict[str, str] | None = None,
    header_values: dict[str, str] | None = None,
    arg_values: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,
) -> None:
    """Save runtime + secrets while honoring the secret-placeholder
    convention: any secret whose value is exactly '***' is left
    untouched (use prior value)."""
    rt = load_runtime(plugin_dir)
    new_rt = rt.model_copy(update={
        "env_values": env_values or {},
        "header_values": header_values or {},
        "arg_values": arg_values or {},
    })
    save_runtime(plugin_dir, new_rt)

    if secrets is not None:
        existing_secrets = load_secrets(plugin_dir)
        merged: dict[str, str] = dict(existing_secrets)
        for k, v in secrets.items():
            if v == SECRET_PLACEHOLDER:
                continue  # leave existing value alone
            merged[k] = v
        save_secrets(plugin_dir, merged)


def serialize_plugin_summary(plugin) -> dict:
    """Used by GET /api/plugins list view."""
    m = plugin.manifest
    return {
        "slug": plugin.directory.name,
        "name": m.name,
        "title": m.title or m.name,
        "version": m.version,
        "description": m.description,
        "category": m.glados_category,
        "icon": m.glados_icon,
        "enabled": plugin.enabled,
        "is_remote": plugin.is_remote(),
    }


def serialize_plugin_detail(plugin) -> dict:
    """Used by GET /api/plugins/<slug>. Secrets returned as '***'."""
    m = plugin.manifest
    secrets_masked = {k: SECRET_PLACEHOLDER for k in plugin.secrets}
    return {
        "slug": plugin.directory.name,
        "manifest": m.model_dump(by_alias=True, exclude_none=True, mode="json"),
        "runtime": plugin.runtime.model_dump(mode="json"),
        "secrets": secrets_masked,
        "is_remote": plugin.is_remote(),
    }
```

- [ ] **Step 4: Add 8 endpoint handlers in `tts_ui.py`**

In `tts_ui.py`, add the import block (near top of file with other imports):

```python
from glados.webui import plugin_endpoints as _plugins
```

Add a module-level reference to the manager (resolved lazily from the engine):

```python
# Near top of tts_ui.py with the other module-level helpers:
def _mcp_manager():
    """Return the live MCPManager from the engine, or None."""
    try:
        from glados.api import api_wrapper as _aw
        engine = getattr(_aw, "_engine", None)
        return getattr(engine, "mcp_manager", None) if engine else None
    except Exception:
        return None
```

Update `do_GET` (around line 1888) to add a new branch BEFORE the final `if not require_perm(self, "admin")` block:

```python
        # Plugin endpoints — admin-only (handled below by the require_perm gate).
        if self.path == "/api/plugins" or self.path.startswith("/api/plugins/"):
            if not require_perm(self, "admin"):
                return
            self._dispatch_plugins_get()
            return
```

Update `do_POST` similarly:

```python
        if self.path == "/api/plugins/install" or (
            self.path.startswith("/api/plugins/")
            and (self.path.endswith("/enable") or self.path.endswith("/disable")
                 or "/" not in self.path[len("/api/plugins/"):])
            # /api/plugins/<slug> save also lands here
        ):
            if not require_perm(self, "admin"):
                return
            self._dispatch_plugins_post()
            return
```

Add `do_DELETE` (new method):

```python
    def do_DELETE(self):
        if self.path.startswith("/api/plugins/"):
            if not require_perm(self, "admin"):
                return
            slug = self.path[len("/api/plugins/"):]
            if slug.endswith("/"):
                slug = slug[:-1]
            self._delete_plugin(slug)
            return
        self._send_error(405, "Method not allowed")
```

Add the dispatch + handler methods at the end of the class:

```python
    # ── Plugins ──────────────────────────────────────────────────

    def _dispatch_plugins_get(self) -> None:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/plugins":
            self._list_plugins()
            return

        # /api/plugins/<slug>
        # /api/plugins/<slug>/logs
        rest = path[len("/api/plugins/"):]
        if "/" in rest:
            slug, _, sub = rest.partition("/")
            if sub == "logs":
                lines = int(parse_qs(parsed.query).get("lines", ["200"])[0])
                self._plugin_logs(slug, min(lines, 5000))
                return
            self._send_error(404, "Not found")
            return

        # /api/plugins/<slug>
        self._plugin_detail(rest)

    def _dispatch_plugins_post(self) -> None:
        path = self.path
        if path == "/api/plugins/install":
            self._install_plugin()
            return

        rest = path[len("/api/plugins/"):]
        if rest.endswith("/enable"):
            self._set_plugin_enabled(rest[:-len("/enable")], True)
            return
        if rest.endswith("/disable"):
            self._set_plugin_enabled(rest[:-len("/disable")], False)
            return
        # /api/plugins/<slug> save
        self._save_plugin_runtime(rest)

    def _list_plugins(self) -> None:
        import os as _os
        enabled_globally = _os.environ.get("GLADOS_PLUGINS_ENABLED", "true").lower() in ("1","true","yes","on")
        if not enabled_globally:
            self._send_json(200, {"plugins": [], "enabled_globally": False})
            return
        plugins = _plugins.discover_plugins()
        out = [_plugins.serialize_plugin_summary(p) for p in plugins]
        self._send_json(200, {"plugins": out, "enabled_globally": True})

    def _plugin_detail(self, slug: str) -> None:
        plugins_dir = _plugins.default_plugins_dir()
        target = plugins_dir / slug
        try:
            plugin = _plugins.load_plugin(target)
        except (_plugins.ManifestError, FileNotFoundError) as exc:
            self._send_json(404, {"error": str(exc)})
            return
        self._send_json(200, _plugins.serialize_plugin_detail(plugin))

    def _install_plugin(self) -> None:
        body = self._read_json_body()
        url = (body.get("url") or "").strip()
        slug_hint = body.get("slug")
        try:
            result = _plugins.install_from_url(url, slug_hint or None)
        except _plugins.InstallError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, result)

    def _save_plugin_runtime(self, slug: str) -> None:
        body = self._read_json_body()
        plugins_dir = _plugins.default_plugins_dir()
        target = plugins_dir / slug
        if not target.exists():
            self._send_json(404, {"error": f"plugin {slug!r} not installed"})
            return
        try:
            _plugins.merge_runtime_save(
                target,
                env_values=body.get("env_values"),
                header_values=body.get("header_values"),
                arg_values=body.get("arg_values"),
                secrets=body.get("secrets"),
            )
        except Exception as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, {"saved": True})

    def _set_plugin_enabled(self, slug: str, enabled: bool) -> None:
        plugins_dir = _plugins.default_plugins_dir()
        target = plugins_dir / slug
        if not target.exists():
            self._send_json(404, {"error": f"plugin {slug!r} not installed"})
            return
        _plugins.set_enabled(target, enabled)

        manager = _mcp_manager()
        if manager is None:
            self._send_json(200, {"enabled": enabled, "session": "manager-unavailable"})
            return

        try:
            if enabled:
                plugin = _plugins.load_plugin(target)
                cfg = _plugins.plugin_to_mcp_config(plugin)
                manager.add_server(cfg)
            else:
                manager.remove_server(slug)
        except Exception as exc:
            logger.warning("hot-rotate {!s} enabled={}: {}", slug, enabled, exc)
            # Runtime state already flipped; surface but don't roll back.
            self._send_json(200, {"enabled": enabled, "session_error": str(exc)})
            return
        self._send_json(200, {"enabled": enabled})

    def _delete_plugin(self, slug: str) -> None:
        plugins_dir = _plugins.default_plugins_dir()
        target = plugins_dir / slug
        manager = _mcp_manager()
        if manager is not None:
            try:
                manager.remove_server(slug)
            except Exception:
                pass
        try:
            _plugins.remove_plugin(plugins_dir, slug)
        except _plugins.InstallError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, {"removed": True})

    def _plugin_logs(self, slug: str, lines: int) -> None:
        import os as _os
        log_dir = _os.environ.get("GLADOS_PLUGIN_LOG_DIR", "/app/logs/plugins")
        log_path = _Path(log_dir) / f"{slug}.log"
        stdio_log: list[str] = []
        if log_path.exists():
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                    stdio_log = fh.readlines()[-lines:]
            except OSError:
                pass

        manager = _mcp_manager()
        events = manager.get_plugin_events(slug, limit=lines) if manager else []
        self._send_json(200, {"stdio_log": stdio_log, "events": events})
```

(The plan assumes `_send_json`, `_read_json_body`, `_send_error`, `_Path` exist in the existing handler — verify and adapt names if they differ. They do; tts_ui.py uses these throughout.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_webui_plugins.py -v`
Expected: 12 passed.

Some test fixtures (`authenticated_admin_client`) may already exist in `tests/conftest.py` — verify; if not, port the pattern from `tests/test_webui_*.py`.

- [ ] **Step 6: Commit**

```bash
git add glados/webui/plugin_endpoints.py glados/webui/tts_ui.py tests/test_webui_plugins.py
git commit -m "feat(webui): /api/plugins/* core 8 endpoints

list / detail / install / save / enable / disable / delete / logs.
admin-only via existing require_perm gate. install enforces https://,
SSRF rejection (loopback + RFC1918 + link-local), 256 KB cap, 5 s
timeout. Save handles secret-placeholder ('***') passthrough so the
WebUI doesn't have to re-enter unchanged secrets. enable/disable
hot-rotates the named plugin via MCPManager.add/remove_server.

Helpers in glados/webui/plugin_endpoints.py keep the install-flow
guards unit-testable independently of the HTTP server."
```

---

## Task 5: ServicesConfig.plugin_indexes field

**Goal:** `cfg.services.plugin_indexes: list[str]` round-trips on `services.yaml` save/load with https-only validation.

**Files:**
- Modify: `glados/core/config_store.py` (ServicesConfig — add field + validator)
- Create: `tests/test_services_config_plugin_indexes.py`

**Acceptance Criteria:**
- [ ] Default value: empty list.
- [ ] Save/load round-trips via the existing `services.yaml` path.
- [ ] Field-level validator rejects non-https URLs at load time with a clear error message; existing in-prod configs without the field continue to load (default empty).
- [ ] 4 new tests pass.

**Verify:** `python -m pytest tests/test_services_config_plugin_indexes.py -v`

**Steps:**

- [ ] **Step 1: Write tests (failing)**

```python
# tests/test_services_config_plugin_indexes.py
"""ServicesConfig.plugin_indexes round-trip + validation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from glados.core.config_store import ServicesConfig


def test_plugin_indexes_default_is_empty():
    cfg = ServicesConfig()
    assert cfg.plugin_indexes == []


def test_plugin_indexes_https_only():
    with pytest.raises(ValidationError, match="https"):
        ServicesConfig(plugin_indexes=["http://x.test/index.json"])


def test_plugin_indexes_accepts_https_list():
    cfg = ServicesConfig(plugin_indexes=[
        "https://raw.githubusercontent.com/synssins/glados-plugins/main/index.json",
        "https://example.test/community/index.json",
    ])
    assert len(cfg.plugin_indexes) == 2


def test_plugin_indexes_round_trip(tmp_path):
    """Save → load via YAML."""
    import yaml
    cfg = ServicesConfig(plugin_indexes=["https://x.test/i.json"])
    dump = cfg.model_dump(mode="json")
    yaml_text = yaml.safe_dump(dump)
    parsed = yaml.safe_load(yaml_text)
    cfg2 = ServicesConfig.model_validate(parsed)
    assert cfg2.plugin_indexes == ["https://x.test/i.json"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_services_config_plugin_indexes.py -v`
Expected: FAIL — field doesn't exist.

- [ ] **Step 3: Add the field to `ServicesConfig` in `glados/core/config_store.py`**

```python
# In ServicesConfig (around line 332 in config_store.py), add:
    plugin_indexes: list[str] = Field(
        default_factory=list,
        description="HTTPS URLs to plugin index.json files. WebUI Browse tab merges these into the catalog.",
    )

    @field_validator("plugin_indexes")
    @classmethod
    def _plugin_indexes_https_only(cls, v: list[str]) -> list[str]:
        for url in v:
            if not url.lower().startswith("https://"):
                raise ValueError(f"plugin index URL must be https://: {url!r}")
        return v
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_services_config_plugin_indexes.py -v`
Expected: 4 passed.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: 1540 passed.

- [ ] **Step 6: Commit**

```bash
git add glados/core/config_store.py tests/test_services_config_plugin_indexes.py
git commit -m "feat(config): ServicesConfig.plugin_indexes — operator-managed plugin index URL list

Stores the list of index.json URLs the WebUI Browse tab merges into the
plugin catalog. Field validator enforces https-only at load time, with
a clear error pointing at the offending URL. Default empty so operators
can opt in to Phase 3's curated repo or roll their own."
```

---

## Task 6: Browse endpoints (`/api/plugins/indexes` + `/api/plugins/browse`)

**Goal:** Three new endpoints: GET/POST `/api/plugins/indexes` (manage URL list) and GET `/api/plugins/browse` (fetch + merge all configured indexes into a catalog).

**Files:**
- Modify: `glados/webui/plugin_endpoints.py` (add browse helpers)
- Modify: `glados/webui/tts_ui.py` (wire 3 endpoints)
- Modify: `tests/test_webui_plugins.py` (extend with browse tests)

**Acceptance Criteria:**
- [ ] `GET /api/plugins/indexes` returns `{urls: [...]}` from `cfg.services.plugin_indexes`.
- [ ] `POST /api/plugins/indexes` accepts `{urls: [...]}`, validates https-only, persists via the existing `_put_config_section("services", ...)` path. Returns saved list.
- [ ] `GET /api/plugins/browse` walks each index URL with the same SSRF + size guards as install; one failed index doesn't fail the whole request (returns partial + per-index `errors: [...]`).
- [ ] Browse merges entries by `name` (last-index-wins). Each entry includes `source_index` so the WebUI can show provenance.
- [ ] 6 new tests pass.

**Verify:** `python -m pytest tests/test_webui_plugins.py::test_browse_* -v`

**Steps:**

- [ ] **Step 1: Append browse helpers to `plugin_endpoints.py`**

```python
# Append to glados/webui/plugin_endpoints.py

INDEX_REQUIRED_KEYS = {"name", "title", "category", "server_json_url"}


def fetch_index(url: str) -> list[dict]:
    """Fetch a single index.json. Raises InstallError on any failure.
    Returns the validated entries list (each dict has at least the
    required keys + source_index = url)."""
    if not url.lower().startswith("https://"):
        raise InstallError("index URL must use https://")
    parsed = urlparse(url)
    if not parsed.hostname or not _resolve_safe_host(parsed.hostname):
        raise InstallError("index host failed SSRF guard")
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True) as client:
            r = client.get(url)
    except httpx.HTTPError as exc:
        raise InstallError(f"index fetch failed: {exc}") from exc
    if r.status_code != 200:
        raise InstallError(f"index fetch returned HTTP {r.status_code}")
    if len(r.content) > MAX_MANIFEST_BYTES:
        raise InstallError("index too large")
    try:
        import json as _json
        raw = _json.loads(r.text)
    except Exception as exc:
        raise InstallError(f"index is not valid JSON: {exc}") from exc

    plugins = raw.get("plugins") if isinstance(raw, dict) else None
    if not isinstance(plugins, list):
        raise InstallError("index missing 'plugins' array")

    out: list[dict] = []
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        if not INDEX_REQUIRED_KEYS.issubset(entry.keys()):
            continue
        if not str(entry["server_json_url"]).lower().startswith("https://"):
            continue
        e = dict(entry)
        e["source_index"] = url
        out.append(e)
    return out


def merge_browse_catalog(index_urls: list[str]) -> dict:
    """Walk every index URL; return {entries: [...], errors: [{url, error}]}.
    One failed index does NOT fail the whole call. Entries deduped by
    name (last-index-wins)."""
    by_name: dict[str, dict] = {}
    errors: list[dict] = []
    for url in index_urls:
        try:
            for entry in fetch_index(url):
                by_name[entry["name"]] = entry
        except InstallError as exc:
            errors.append({"url": url, "error": str(exc)})
    return {"entries": list(by_name.values()), "errors": errors}
```

- [ ] **Step 2: Add 3 endpoint handlers + dispatcher branches in `tts_ui.py`**

Update `_dispatch_plugins_get` to recognize `indexes` and `browse`:

```python
    def _dispatch_plugins_get(self) -> None:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/plugins":
            self._list_plugins()
            return
        if path == "/api/plugins/indexes":
            self._get_plugin_indexes()
            return
        if path == "/api/plugins/browse":
            self._browse_plugins()
            return

        rest = path[len("/api/plugins/"):]
        if "/" in rest:
            slug, _, sub = rest.partition("/")
            if sub == "logs":
                lines = int(parse_qs(parsed.query).get("lines", ["200"])[0])
                self._plugin_logs(slug, min(lines, 5000))
                return
            self._send_error(404, "Not found")
            return

        self._plugin_detail(rest)
```

Update `_dispatch_plugins_post` to recognize `indexes`:

```python
    def _dispatch_plugins_post(self) -> None:
        path = self.path
        if path == "/api/plugins/install":
            self._install_plugin()
            return
        if path == "/api/plugins/indexes":
            self._set_plugin_indexes()
            return
        # ... rest unchanged
```

Add the three new methods:

```python
    def _get_plugin_indexes(self) -> None:
        from glados.core.config_store import cfg
        urls = list(cfg.services.plugin_indexes)
        self._send_json(200, {"urls": urls})

    def _set_plugin_indexes(self) -> None:
        body = self._read_json_body()
        urls = body.get("urls", [])
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            self._send_json(400, {"error": "urls must be a list of strings"})
            return
        for url in urls:
            if not url.lower().startswith("https://"):
                self._send_json(400, {"error": f"non-https URL rejected: {url!r}"})
                return
        # Persist via the existing services-section save path.
        try:
            self._put_config_section("services", {"plugin_indexes": urls})
        except Exception as exc:
            self._send_json(500, {"error": f"persist failed: {exc}"})
            return
        self._send_json(200, {"urls": urls})

    def _browse_plugins(self) -> None:
        from glados.core.config_store import cfg
        urls = list(cfg.services.plugin_indexes)
        if not urls:
            self._send_json(200, {"entries": [], "errors": []})
            return
        result = _plugins.merge_browse_catalog(urls)
        self._send_json(200, result)
```

- [ ] **Step 3: Add browse tests**

Append to `tests/test_webui_plugins.py`:

```python
def test_browse_indexes_get_returns_configured_urls(authenticated_admin_client, monkeypatch):
    from glados.core.config_store import cfg
    monkeypatch.setattr(cfg.services, "plugin_indexes", ["https://x.test/i.json"])
    r = authenticated_admin_client.get("/api/plugins/indexes")
    assert r.status_code == 200
    assert r.json() == {"urls": ["https://x.test/i.json"]}


def test_browse_indexes_post_https_only(authenticated_admin_client):
    r = authenticated_admin_client.post(
        "/api/plugins/indexes",
        json={"urls": ["http://x.test/i.json"]},
    )
    assert r.status_code == 400


def test_browse_endpoint_merges_indexes(authenticated_admin_client, monkeypatch):
    from glados.core.config_store import cfg
    monkeypatch.setattr(cfg.services, "plugin_indexes", [
        "https://a.test/i.json", "https://b.test/i.json",
    ])

    def fake_fetch(url):
        if url == "https://a.test/i.json":
            return [{"name": "p1", "title": "P One", "category": "media",
                     "server_json_url": "https://a.test/p1.json", "source_index": url}]
        return [{"name": "p2", "title": "P Two", "category": "dev",
                 "server_json_url": "https://b.test/p2.json", "source_index": url}]

    with patch("glados.webui.plugin_endpoints.fetch_index", side_effect=fake_fetch):
        r = authenticated_admin_client.get("/api/plugins/browse")
    assert r.status_code == 200
    body = r.json()
    assert {e["name"] for e in body["entries"]} == {"p1", "p2"}
    assert body["errors"] == []


def test_browse_endpoint_partial_on_one_failure(authenticated_admin_client, monkeypatch):
    from glados.core.config_store import cfg
    from glados.plugins.errors import InstallError
    monkeypatch.setattr(cfg.services, "plugin_indexes", [
        "https://a.test/i.json", "https://b.test/i.json",
    ])

    def fake_fetch(url):
        if url == "https://a.test/i.json":
            return [{"name": "p1", "title": "P One", "category": "media",
                     "server_json_url": "https://a.test/p1.json", "source_index": url}]
        raise InstallError("boom")

    with patch("glados.webui.plugin_endpoints.fetch_index", side_effect=fake_fetch):
        r = authenticated_admin_client.get("/api/plugins/browse")
    assert r.status_code == 200
    body = r.json()
    assert len(body["entries"]) == 1
    assert len(body["errors"]) == 1
    assert "boom" in body["errors"][0]["error"]


def test_browse_dedupe_last_index_wins(authenticated_admin_client, monkeypatch):
    from glados.core.config_store import cfg
    monkeypatch.setattr(cfg.services, "plugin_indexes", [
        "https://a.test/i.json", "https://b.test/i.json",
    ])

    def fake_fetch(url):
        return [{"name": "p1", "title": f"From {url}", "category": "x",
                 "server_json_url": f"{url}/p1.json", "source_index": url}]

    with patch("glados.webui.plugin_endpoints.fetch_index", side_effect=fake_fetch):
        r = authenticated_admin_client.get("/api/plugins/browse")
    body = r.json()
    assert len(body["entries"]) == 1
    assert body["entries"][0]["title"] == "From https://b.test/i.json"


def test_browse_empty_when_no_indexes(authenticated_admin_client, monkeypatch):
    from glados.core.config_store import cfg
    monkeypatch.setattr(cfg.services, "plugin_indexes", [])
    r = authenticated_admin_client.get("/api/plugins/browse")
    assert r.status_code == 200
    assert r.json() == {"entries": [], "errors": []}
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_webui_plugins.py -k browse -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add glados/webui/plugin_endpoints.py glados/webui/tts_ui.py tests/test_webui_plugins.py
git commit -m "feat(webui): /api/plugins/indexes + /api/plugins/browse

Operator manages a list of index.json URLs via GET/POST
/api/plugins/indexes (persisted under cfg.services.plugin_indexes).
GET /api/plugins/browse walks every URL, merges entries by name
(last-index-wins), tags each with source_index for provenance, and
returns partial results + per-index errors when one index fails.
Same SSRF + size guards as the install endpoint. Phase 3 will ship
the curated synssins/glados-plugins index that operators can add to
this list."
```

---

## Task 7: WebUI panel skeleton — installed list + toggle + trash + off-state

**Goal:** New "Plugins" sub-section under System → Services rendering the installed list with row layout `[icon] Name vX.Y.Z [cat] ●  [⏻ toggle]  [⚙]  [🗑]`. Off-state when `enabled_globally: false`.

**Files:**
- Modify: `glados/webui/static/ui.js` (add functions ~120 lines)

**Acceptance Criteria:**
- [ ] Plugins card appears below the LLM Endpoints card on System → Services tab.
- [ ] When `GLADOS_PLUGINS_ENABLED=false`: card renders the "Plugins disabled — set `GLADOS_PLUGINS_ENABLED=true` in compose to enable" notice and nothing else.
- [ ] Each plugin row renders icon + name + version + category badge + status dot + Enabled toggle + gear icon (no-op stub for T8) + trash icon.
- [ ] Toggling Enabled calls `/api/plugins/<slug>/enable` or `/disable`. Optimistic UI update with revert on failure. Status dot updates from API response.
- [ ] Trash icon opens existing confirm dialog ("Delete plugin <name>? This will remove its configuration permanently."). Confirmed → DELETE /api/plugins/<slug> → row disappears.
- [ ] Polls `/api/plugins` every 30 s while the System → Services tab is visible (matches existing pattern).

**Verify:** Manual UI inspection on deploy. No new pytest needed (UI behavior).

**Steps:**

- [ ] **Step 1: Locate the existing System → Services renderer**

`loadSystemServices()` in ui.js lives around line 2213. The function ends with `'</div>'` after rendering the LLM Endpoints card. Append the Plugins card render call.

- [ ] **Step 2: Add the Plugins card hook + renderer**

Insert at the end of `loadSystemServices()`, just before its closing `}`:

```javascript
  // Plugins card — Phase 2b. Renders below LLM Endpoints.
  html += '<div class="card" style="margin-top:var(--sp-3);" id="plugins-card-host">';
  html +=   '<div class="section-title">Plugins</div>';
  html +=   '<div id="plugins-panel-body"><div class="mode-desc">Loading…</div></div>';
  html += '</div>';

  body.innerHTML = html;

  // Existing LLM rendering ...
  cfgRenderServices({ services: pluginsLLMSubset(svc) });

  // Plugins panel — kick off async load.
  loadPluginsPanel();
```

(If `body.innerHTML = html` already exists earlier in `loadSystemServices`, splice the Plugins card div in BEFORE that assignment.)

- [ ] **Step 3: Add `loadPluginsPanel`, `renderPluginsList`, and helpers**

Add at module scope in ui.js (near other System → Services functions):

```javascript
// ── Plugins panel (Phase 2b) ───────────────────────────────────────

let _pluginsPollTimer = null;

async function loadPluginsPanel() {
  const host = document.getElementById('plugins-panel-body');
  if (!host) return;
  try {
    const r = await fetch('/api/plugins', { credentials: 'same-origin' });
    if (!r.ok) {
      host.innerHTML = '<div class="mode-desc" style="color:var(--fg-muted);">' +
        'Failed to load plugins (' + r.status + ')</div>';
      return;
    }
    const data = await r.json();
    if (!data.enabled_globally) {
      host.innerHTML = renderPluginsOffNotice();
      return;
    }
    host.innerHTML = renderPluginsList(data.plugins) + renderAddByUrlCard() + renderBrowseCard();
    wirePluginRowHandlers();
    wireAddByUrlHandlers();
    wireBrowseHandlers();
    schedulePluginsPoll();
  } catch (e) {
    host.innerHTML = '<div class="mode-desc">Plugins panel error: ' +
      (e && e.message ? e.message : String(e)) + '</div>';
  }
}

function renderPluginsOffNotice() {
  return '<div class="mode-desc" style="padding:18px 20px;border:1px dashed var(--border);">' +
    '<strong>Plugins disabled.</strong> Set <code>GLADOS_PLUGINS_ENABLED=true</code> in ' +
    'docker-compose.yml and restart the container to enable. ' +
    '<a href="/docs/plugins-architecture.md" target="_blank">Architecture doc.</a>' +
    '</div>';
}

function renderPluginsList(plugins) {
  if (!plugins || plugins.length === 0) {
    return '<div class="mode-desc" style="padding:18px 20px;">' +
      'No plugins installed yet. Use the "Add by URL" card below or "Browse" to install one.' +
      '</div>';
  }
  let h = '<div class="plugin-list">';
  for (const p of plugins) {
    const dotClass = p.enabled ? 'dot-on' : 'dot-off';
    const toggleAttr = p.enabled ? 'checked' : '';
    h += '<div class="plugin-row" data-slug="' + p.slug + '">';
    h +=   '<span class="plugin-icon">' + iconSvg(p.icon || 'plug') + '</span>';
    h +=   '<span class="plugin-name">' + escapeHtml(p.title || p.name) + '</span>';
    h +=   '<span class="plugin-version">v' + escapeHtml(p.version) + '</span>';
    h +=   '<span class="plugin-cat-badge">' + escapeHtml(p.category) + '</span>';
    h +=   '<span class="plugin-status-dot ' + dotClass + '"></span>';
    h +=   '<label class="switch"><input type="checkbox" data-action="toggle-enabled" ' + toggleAttr + '><span class="slider"></span></label>';
    h +=   '<button class="icon-btn" data-action="open-config" title="Configure">' + iconSvg('settings') + '</button>';
    h +=   '<button class="icon-btn icon-btn-danger" data-action="delete-plugin" title="Delete">' + iconSvg('trash-2') + '</button>';
    h += '</div>';
  }
  h += '</div>';
  return h;
}

function wirePluginRowHandlers() {
  document.querySelectorAll('.plugin-row').forEach(row => {
    const slug = row.getAttribute('data-slug');

    // Enable/disable toggle
    const toggle = row.querySelector('input[data-action="toggle-enabled"]');
    if (toggle) {
      toggle.addEventListener('change', async (ev) => {
        const newState = ev.target.checked;
        const path = newState ? 'enable' : 'disable';
        ev.target.disabled = true;
        try {
          const r = await fetch('/api/plugins/' + encodeURIComponent(slug) + '/' + path, {
            method: 'POST', credentials: 'same-origin',
          });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          toast(newState ? 'Plugin enabled' : 'Plugin disabled', 'ok');
          // Refresh status dot from server.
          await loadPluginsPanel();
        } catch (e) {
          ev.target.checked = !newState;  // revert
          toast('Toggle failed: ' + e.message, 'err');
        } finally {
          ev.target.disabled = false;
        }
      });
    }

    // Gear → open config modal (T8). Stub for now.
    const gear = row.querySelector('button[data-action="open-config"]');
    if (gear) {
      gear.addEventListener('click', () => openPluginConfigModal(slug));
    }

    // Trash → confirm + delete.
    const trash = row.querySelector('button[data-action="delete-plugin"]');
    if (trash) {
      trash.addEventListener('click', async () => {
        const ok = await confirmDialog({
          title: 'Delete plugin?',
          body: 'This will permanently remove the plugin and all its configuration.',
          confirmLabel: 'Delete',
          danger: true,
        });
        if (!ok) return;
        try {
          const r = await fetch('/api/plugins/' + encodeURIComponent(slug), {
            method: 'DELETE', credentials: 'same-origin',
          });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          toast('Plugin deleted', 'ok');
          await loadPluginsPanel();
        } catch (e) {
          toast('Delete failed: ' + e.message, 'err');
        }
      });
    }
  });
}

function schedulePluginsPoll() {
  if (_pluginsPollTimer) clearInterval(_pluginsPollTimer);
  _pluginsPollTimer = setInterval(() => {
    if (document.hidden) return;
    if (currentPage() !== 'system') return;  // only when System is visible
    loadPluginsPanel();
  }, 30000);
}

// Stubs filled in by T8/T10/T11. Defined here so wire-up doesn't
// throw before those tasks land.
function openPluginConfigModal(slug) { /* T8 */ }
function renderAddByUrlCard() { return ''; /* T10 */ }
function wireAddByUrlHandlers() { /* T10 */ }
function renderBrowseCard() { return ''; /* T11 */ }
function wireBrowseHandlers() { /* T11 */ }
```

- [ ] **Step 4: Add CSS for the new row layout**

Append to `glados/webui/static/style.css` (or whatever the existing stylesheet is — verify path):

```css
.plugin-list { display: flex; flex-direction: column; gap: 8px; padding: 12px 14px; }
.plugin-row {
  display: grid;
  grid-template-columns: 28px 1fr auto auto 18px 60px 32px 32px;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-elev);
}
.plugin-icon { color: var(--fg-secondary); }
.plugin-name { font-weight: 500; }
.plugin-version { color: var(--fg-muted); font-family: var(--font-mono); font-size: 0.8rem; }
.plugin-cat-badge {
  background: var(--bg-recessed); color: var(--fg-secondary);
  padding: 2px 8px; border-radius: 10px; font-size: 0.75rem;
}
.plugin-status-dot { width: 10px; height: 10px; border-radius: 50%; }
.plugin-status-dot.dot-on  { background: var(--ok); }
.plugin-status-dot.dot-off { background: var(--fg-muted); }
.plugin-status-dot.dot-err { background: var(--danger); }
.icon-btn {
  background: none; border: none; cursor: pointer; padding: 4px;
  color: var(--fg-secondary); border-radius: 4px;
}
.icon-btn:hover { background: var(--bg-elev-hover); }
.icon-btn-danger:hover { color: var(--danger); }
```

- [ ] **Step 5: Verify in deploy (deferred to T13)**

No standalone deploy here — the panel will be exercised end-to-end at T13. No automated test for this task; manual verification on deploy.

- [ ] **Step 6: Commit**

```bash
git add glados/webui/static/ui.js glados/webui/static/style.css
git commit -m "feat(webui): plugins panel skeleton — installed list, toggle, trash, off-state

Renders below the LLM Endpoints card on System → Services. Off-state
notice when GLADOS_PLUGINS_ENABLED=false. Per-row layout matches the
spec: icon, name+version, category badge, status dot, Enabled toggle
(hot-rotates via /api/plugins/<slug>/enable|disable), gear (T8 stub),
trash (confirm dialog → DELETE).

Polls /api/plugins every 30 s while the tab is visible — same pattern
as the existing service-health dot polling. Add-by-URL and Browse
cards stubbed; populated in T10 and T11."
```

---

## Task 8: Configuration modal — Configuration tab

**Goal:** Gear icon opens a modal with three tabs (Configuration / Logs / About). Configuration tab auto-renders the install form from `server.json` + current runtime values, masks secrets as `***`, saves via `POST /api/plugins/<slug>`.

**Files:**
- Modify: `glados/webui/static/ui.js` (replace `openPluginConfigModal` stub + add ~200 lines)
- Modify: `glados/webui/static/style.css` (modal styles)

**Acceptance Criteria:**
- [ ] Gear icon opens a centered modal with dimmed backdrop. Esc / click-outside / Cancel closes; with-confirm if dirty.
- [ ] Three tab buttons across the top: Configuration / Logs / About. Active tab has visual indication.
- [ ] Configuration tab renders three subgroups based on the manifest: `Environment variables` (from `packages[i].environmentVariables[]`), `Headers` (from `remotes[i].headers[]`), `Arguments` (from `packages[i].packageArguments[]` if any).
- [ ] Form rendering: secrets → `<input type="password">`; choices → `<select>`; format=url → `<input type="url">`; isRequired → red asterisk; default → placeholder text.
- [ ] Save button posts `{env_values, header_values, arg_values, secrets}`. Secrets the operator didn't change post as `***` and the server preserves them.
- [ ] Save success → toast + close. Save failure → inline error.

**Verify:** Manual UI test on deploy. Add one DOM-snapshot test with jsdom if test infra supports it (skip if not).

**Steps:**

- [ ] **Step 1: Replace the `openPluginConfigModal` stub**

```javascript
async function openPluginConfigModal(slug) {
  // Fetch detail
  let detail;
  try {
    const r = await fetch('/api/plugins/' + encodeURIComponent(slug),
                          { credentials: 'same-origin' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    detail = await r.json();
  } catch (e) {
    toast('Failed to load plugin: ' + e.message, 'err');
    return;
  }

  // Build modal DOM
  const modal = createModal({
    title: detail.manifest.title || detail.manifest.name,
    body: renderPluginModalBody(detail),
    width: 720,
  });

  // Wire tab switching
  modal.body.querySelectorAll('[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => switchPluginTab(modal, btn.dataset.tab));
  });

  // Wire Save
  const saveBtn = modal.body.querySelector('[data-action="save-plugin"]');
  if (saveBtn) {
    saveBtn.addEventListener('click', () => savePluginRuntime(slug, modal));
  }

  // Default tab
  switchPluginTab(modal, 'config');

  modal.show();
}

function renderPluginModalBody(detail) {
  const m = detail.manifest;
  let h = '';
  // Tabs
  h += '<div class="modal-tabs">';
  h += '  <button class="modal-tab" data-tab="config">Configuration</button>';
  h += '  <button class="modal-tab" data-tab="logs">Logs</button>';
  h += '  <button class="modal-tab" data-tab="about">About</button>';
  h += '</div>';

  // Configuration tab pane
  h += '<div class="modal-pane" data-pane="config">';
  h += renderConfigForm(detail);
  h += '<div class="modal-actions">';
  h += '  <span class="save-result" data-role="save-result"></span>';
  h += '  <button class="btn-secondary" data-action="close-modal">Cancel</button>';
  h += '  <button class="btn-primary" data-action="save-plugin">Save</button>';
  h += '</div>';
  h += '</div>';

  // Logs + About panes — populated in T9.
  h += '<div class="modal-pane" data-pane="logs"><div class="mode-desc">Loading logs…</div></div>';
  h += '<div class="modal-pane" data-pane="about">' + renderAboutPane(detail) + '</div>';
  return h;
}

function renderConfigForm(detail) {
  const m = detail.manifest;
  const rt = detail.runtime;
  const secretMask = detail.secrets || {};

  // Pick the active package or remote based on runtime indices.
  const pkg = (rt.package_index !== null && rt.package_index !== undefined)
              ? m.packages[rt.package_index] : null;
  const remote = (rt.remote_index !== null && rt.remote_index !== undefined)
                 ? m.remotes[rt.remote_index] : null;

  let h = '<form class="plugin-config-form" data-role="config-form">';

  if (pkg && pkg.environmentVariables && pkg.environmentVariables.length) {
    h += '<div class="form-section">';
    h += '<h4>Environment variables</h4>';
    for (const ev of pkg.environmentVariables) {
      h += renderFormField('env', ev, rt.env_values || {}, secretMask);
    }
    h += '</div>';
  }

  if (remote && remote.headers && remote.headers.length) {
    h += '<div class="form-section">';
    h += '<h4>Headers</h4>';
    for (const hd of remote.headers) {
      h += renderFormField('header', hd, rt.header_values || {}, secretMask);
    }
    h += '</div>';
  }

  if (pkg && pkg.packageArguments && pkg.packageArguments.length) {
    const visible = pkg.packageArguments.filter(a => !a.value);  // ignore hard-coded
    if (visible.length) {
      h += '<div class="form-section">';
      h += '<h4>Arguments</h4>';
      for (const arg of visible) {
        h += renderFormField('arg', arg, rt.arg_values || {}, secretMask);
      }
      h += '</div>';
    }
  }

  h += '</form>';
  return h;
}

function renderFormField(group, spec, values, secretMask) {
  const name = spec.name || spec.value_hint || '';
  if (!name) return '';
  const value = values[name] !== undefined ? values[name] : '';
  const requiredMark = spec.is_required ? '<span class="required-mark">*</span>' : '';
  const placeholder = spec.default ? ('default: ' + spec.default) : '';
  const description = spec.description || '';

  let inputHtml;
  if (spec.is_secret) {
    const mask = secretMask[name] !== undefined ? '***' : '';
    inputHtml = '<input type="password" name="' + escapeHtml(name) +
                '" data-group="' + group + '" data-secret="1" value="' +
                escapeHtml(mask) + '" placeholder="' + escapeHtml(placeholder) + '">';
  } else if (spec.choices) {
    let opts = '<option value=""></option>';
    for (const c of spec.choices) {
      const sel = (c === value) ? ' selected' : '';
      opts += '<option' + sel + ' value="' + escapeHtml(c) + '">' + escapeHtml(c) + '</option>';
    }
    inputHtml = '<select name="' + escapeHtml(name) + '" data-group="' + group + '">' + opts + '</select>';
  } else if (spec.format === 'url') {
    inputHtml = '<input type="url" name="' + escapeHtml(name) +
                '" data-group="' + group + '" value="' + escapeHtml(value) +
                '" placeholder="' + escapeHtml(placeholder) + '">';
  } else {
    inputHtml = '<input type="text" name="' + escapeHtml(name) +
                '" data-group="' + group + '" value="' + escapeHtml(value) +
                '" placeholder="' + escapeHtml(placeholder) + '">';
  }
  return '<div class="form-field">' +
         '  <label>' + escapeHtml(name) + requiredMark + '</label>' +
         '  ' + inputHtml +
         '  ' + (description ? '<div class="field-desc">' + escapeHtml(description) + '</div>' : '') +
         '</div>';
}

function switchPluginTab(modal, tabName) {
  modal.body.querySelectorAll('.modal-tab').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tabName);
  });
  modal.body.querySelectorAll('.modal-pane').forEach(p => {
    p.style.display = (p.dataset.pane === tabName) ? 'block' : 'none';
  });
  if (tabName === 'logs') {
    loadPluginLogs(modal);  // T9
  }
}

async function savePluginRuntime(slug, modal) {
  const form = modal.body.querySelector('[data-role="config-form"]');
  const result = modal.body.querySelector('[data-role="save-result"]');
  if (!form) return;

  const env_values = {};
  const header_values = {};
  const arg_values = {};
  const secrets = {};

  form.querySelectorAll('input, select').forEach(el => {
    const grp = el.dataset.group;
    const name = el.name;
    const isSecret = el.dataset.secret === '1';
    const v = el.value;
    if (!name) return;
    if (isSecret) {
      // Empty + masked → leave alone. Operator typed real new value → send.
      if (v === '***' || v === '') {
        secrets[name] = '***';  // sentinel: preserve
      } else {
        secrets[name] = v;
      }
      return;
    }
    if (grp === 'env') env_values[name] = v;
    else if (grp === 'header') header_values[name] = v;
    else if (grp === 'arg') arg_values[name] = v;
  });

  result.textContent = 'Saving…';
  try {
    const r = await fetch('/api/plugins/' + encodeURIComponent(slug), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ env_values, header_values, arg_values, secrets }),
    });
    if (!r.ok) throw new Error((await r.json()).error || ('HTTP ' + r.status));
    result.textContent = '';
    toast('Plugin saved', 'ok');
    modal.close();
    loadPluginsPanel();
  } catch (e) {
    result.textContent = 'Save failed: ' + e.message;
    result.style.color = 'var(--danger)';
  }
}
```

- [ ] **Step 2: Add modal helper if not present**

`createModal({title, body, width})` may already exist — check ui.js for an existing modal pattern (likely from Memory page confirm dialogs). If not, add:

```javascript
function createModal({ title, body, width }) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML =
    '<div class="modal-box" style="width:' + (width || 600) + 'px;">' +
    '  <div class="modal-header">' +
    '    <h3 class="modal-title">' + escapeHtml(title || '') + '</h3>' +
    '    <button class="icon-btn" data-action="close-modal">×</button>' +
    '  </div>' +
    '  <div class="modal-body">' + body + '</div>' +
    '</div>';
  const close = () => {
    document.removeEventListener('keydown', escHandler);
    overlay.remove();
  };
  const escHandler = (ev) => { if (ev.key === 'Escape') close(); };

  overlay.addEventListener('click', (ev) => {
    if (ev.target === overlay || ev.target.dataset.action === 'close-modal') close();
  });

  return {
    body: overlay,
    show() {
      document.body.appendChild(overlay);
      document.addEventListener('keydown', escHandler);
    },
    close,
  };
}
```

- [ ] **Step 3: Add CSS**

```css
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.5);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
}
.modal-box {
  background: var(--bg-base); border: 1px solid var(--border);
  border-radius: 8px; box-shadow: 0 8px 32px rgba(0,0,0,0.3);
  max-height: 80vh; display: flex; flex-direction: column;
}
.modal-header { display: flex; justify-content: space-between; align-items: center; padding: 14px 20px; border-bottom: 1px solid var(--border); }
.modal-body { padding: 18px 20px; overflow-y: auto; }
.modal-tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 16px; }
.modal-tab {
  background: none; border: none; padding: 10px 16px; cursor: pointer;
  color: var(--fg-secondary); border-bottom: 2px solid transparent;
}
.modal-tab.active { color: var(--fg-primary); border-bottom-color: var(--accent); }
.modal-pane { display: none; }
.form-section { margin-bottom: 18px; }
.form-section h4 { margin: 0 0 8px; font-size: 0.85rem; color: var(--fg-secondary); text-transform: uppercase; letter-spacing: 0.06em; }
.form-field { display: grid; grid-template-columns: 1fr 2fr; gap: 8px 12px; align-items: center; margin-bottom: 8px; }
.form-field input, .form-field select { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-elev); }
.field-desc { grid-column: 2; font-size: 0.8rem; color: var(--fg-muted); }
.required-mark { color: var(--danger); margin-left: 3px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; padding-top: 14px; border-top: 1px solid var(--border); margin-top: 14px; }
```

- [ ] **Step 4: Commit**

```bash
git add glados/webui/static/ui.js glados/webui/static/style.css
git commit -m "feat(webui): plugin configuration modal — Config tab with form auto-render

Gear icon opens a centered modal with three tabs (Config / Logs / About).
Configuration tab auto-renders environmentVariables[] + headers[] +
packageArguments[] from server.json into typed form fields:
- isSecret → password input, masked '***' on read
- choices  → select
- format=url → url input
- isRequired → red asterisk
- default → placeholder text only

Save posts {env_values, header_values, arg_values, secrets} to
/api/plugins/<slug>; secrets unchanged at the client (still '***')
preserve via the server-side merge logic from T4. Logs + About panes
stubbed; T9 fills them."
```

---

## Task 9: Modal — Logs tab + About tab

**Goal:** Logs tab shows the per-plugin stdio tail + event ring with manual + 5 s auto-refresh. About tab shows version / category / repo URL / source index / "Reinstall from source" button.

**Files:**
- Modify: `glados/webui/static/ui.js` (replace `loadPluginLogs` stub + add `renderAboutPane`)

**Acceptance Criteria:**
- [ ] Logs tab: lines-back selector (100 / 500 / 2000), Refresh button, 5 s auto-refresh toggle. Renders stdio_log lines + events list with timestamps.
- [ ] Auto-refresh tears down on tab switch / modal close.
- [ ] About tab shows: name, version, category, persona role, source URL (where installed from), repository link, Reinstall button.
- [ ] Reinstall button: confirm + DELETE current + POST install with the saved source URL.

**Verify:** Manual UI test on deploy.

**Steps:**

- [ ] **Step 1: Replace `loadPluginLogs` stub**

```javascript
let _pluginLogsAutoTimer = null;

async function loadPluginLogs(modal) {
  const pane = modal.body.querySelector('[data-pane="logs"]');
  if (!pane) return;
  // Build controls once
  if (!pane.dataset.built) {
    pane.innerHTML =
      '<div class="logs-controls" style="display:flex;gap:10px;align-items:center;margin-bottom:10px;">' +
      '  <label>Lines: <select data-role="logs-lines">' +
      '    <option value="100">100</option>' +
      '    <option value="500" selected>500</option>' +
      '    <option value="2000">2000</option>' +
      '  </select></label>' +
      '  <button class="btn-secondary" data-role="logs-refresh">Refresh</button>' +
      '  <label><input type="checkbox" data-role="logs-auto"> Auto-refresh (5 s)</label>' +
      '</div>' +
      '<div class="logs-output">' +
      '  <h4>stderr</h4>' +
      '  <pre data-role="logs-stdio" class="logs-pre"></pre>' +
      '  <h4>events</h4>' +
      '  <div data-role="logs-events" class="logs-events"></div>' +
      '</div>';
    pane.dataset.built = '1';

    pane.querySelector('[data-role="logs-refresh"]').addEventListener('click',
      () => fetchPluginLogsInto(modal));
    pane.querySelector('[data-role="logs-auto"]').addEventListener('change', (ev) => {
      if (_pluginLogsAutoTimer) { clearInterval(_pluginLogsAutoTimer); _pluginLogsAutoTimer = null; }
      if (ev.target.checked) {
        _pluginLogsAutoTimer = setInterval(() => {
          if (!document.body.contains(modal.body)) {
            clearInterval(_pluginLogsAutoTimer); _pluginLogsAutoTimer = null;
            return;
          }
          fetchPluginLogsInto(modal);
        }, 5000);
      }
    });
  }
  await fetchPluginLogsInto(modal);
}

async function fetchPluginLogsInto(modal) {
  // Slug stashed in modal title. Better: stash on the modal itself when opening.
  const slug = modal.body.querySelector('[data-plugin-slug]')?.dataset.pluginSlug;
  if (!slug) return;
  const linesEl = modal.body.querySelector('[data-role="logs-lines"]');
  const lines = linesEl ? linesEl.value : '500';
  try {
    const r = await fetch('/api/plugins/' + encodeURIComponent(slug) + '/logs?lines=' + lines,
                          { credentials: 'same-origin' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const pre = modal.body.querySelector('[data-role="logs-stdio"]');
    if (pre) pre.textContent = (data.stdio_log || []).join('');
    const evDiv = modal.body.querySelector('[data-role="logs-events"]');
    if (evDiv) {
      evDiv.innerHTML = (data.events || []).map(e =>
        '<div class="event-row event-' + e.kind + '">' +
        '<span class="event-ts">' + new Date(e.ts * 1000).toISOString() + '</span> ' +
        '<span class="event-kind">' + escapeHtml(e.kind) + '</span> ' +
        '<span class="event-msg">' + escapeHtml(e.message) + '</span>' +
        '</div>'
      ).join('');
    }
  } catch (e) {
    const pre = modal.body.querySelector('[data-role="logs-stdio"]');
    if (pre) pre.textContent = 'Failed to load logs: ' + e.message;
  }
}
```

Update `openPluginConfigModal` to stash the slug for the logs handler:

```javascript
// In openPluginConfigModal, after createModal returns:
modal.body.dataset.pluginSlug = slug;
// Or add a hidden marker element:
const marker = document.createElement('div');
marker.dataset.pluginSlug = slug;
marker.style.display = 'none';
modal.body.querySelector('.modal-body').prepend(marker);
```

- [ ] **Step 2: Implement `renderAboutPane`**

```javascript
function renderAboutPane(detail) {
  const m = detail.manifest;
  const rt = detail.runtime;
  const role = (m._meta && m._meta['com.synssins.glados/recommended_persona_role']) || 'both';
  const cat = (m._meta && m._meta['com.synssins.glados/category']) || 'utility';
  const repo = m.repository ? m.repository.url : null;
  const sourceUrl = (m._meta && m._meta['com.synssins.glados/source_url']) || null;

  let h = '<div class="about-pane">';
  h += '<dl class="about-list">';
  h += '<dt>Name</dt><dd>' + escapeHtml(m.name) + '</dd>';
  h += '<dt>Title</dt><dd>' + escapeHtml(m.title || m.name) + '</dd>';
  h += '<dt>Version</dt><dd><code>' + escapeHtml(m.version) + '</code></dd>';
  h += '<dt>Description</dt><dd>' + escapeHtml(m.description) + '</dd>';
  h += '<dt>Category</dt><dd>' + escapeHtml(cat) + '</dd>';
  h += '<dt>Persona role</dt><dd>' + escapeHtml(role) + '</dd>';
  if (repo) {
    h += '<dt>Repository</dt><dd><a href="' + escapeHtml(repo) +
         '" target="_blank" rel="noopener">' + escapeHtml(repo) + '</a></dd>';
  }
  if (sourceUrl) {
    h += '<dt>Installed from</dt><dd><a href="' + escapeHtml(sourceUrl) +
         '" target="_blank" rel="noopener">' + escapeHtml(sourceUrl) + '</a></dd>';
  }
  h += '<dt>Transport</dt><dd>' + (rt.remote_index !== null ? 'remote' : 'local') + '</dd>';
  h += '</dl>';
  if (sourceUrl) {
    h += '<button class="btn-secondary" data-action="reinstall-from-source">' +
         'Reinstall from source</button>';
  }
  h += '</div>';
  return h;
}
```

(NOTE: `source_url` in `_meta` is a non-spec extension we record on install. Add to install_from_url in T4 — but T4 already shipped, so this is a small T9 follow-up: store the install URL when writing `server.json` by stashing it in `_meta["com.synssins.glados/source_url"]` before writing. Update `install_from_url` accordingly.)

- [ ] **Step 3: Patch install to record `source_url` in `_meta`**

In `glados/webui/plugin_endpoints.py:install_from_url`, after `manifest = fetch_manifest(url)`, before `install_plugin(...)`:

```python
    # Stash the source URL in _meta so the WebUI About tab can offer
    # "Reinstall from source" later. Reverse-DNS namespace per spec.
    meta = dict(manifest.meta or {})
    meta["com.synssins.glados/source_url"] = url
    manifest = manifest.model_copy(update={"meta": meta})
```

- [ ] **Step 4: Wire reinstall button**

In `openPluginConfigModal`'s click-binding loop, add:

```javascript
  const reinstallBtn = modal.body.querySelector('[data-action="reinstall-from-source"]');
  if (reinstallBtn) {
    reinstallBtn.addEventListener('click', async () => {
      const sourceUrl = detail.manifest._meta &&
                        detail.manifest._meta['com.synssins.glados/source_url'];
      if (!sourceUrl) return;
      const ok = await confirmDialog({
        title: 'Reinstall from source?',
        body: 'This will delete the current plugin and re-install from <code>' +
              escapeHtml(sourceUrl) + '</code>. Configuration values will be lost.',
        confirmLabel: 'Reinstall',
        danger: false,
      });
      if (!ok) return;
      try {
        const dr = await fetch('/api/plugins/' + encodeURIComponent(slug),
                               { method: 'DELETE', credentials: 'same-origin' });
        if (!dr.ok) throw new Error('delete: HTTP ' + dr.status);
        const ir = await fetch('/api/plugins/install', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ url: sourceUrl, slug: slug }),
        });
        if (!ir.ok) throw new Error('install: HTTP ' + ir.status);
        toast('Plugin reinstalled', 'ok');
        modal.close();
        loadPluginsPanel();
      } catch (e) {
        toast('Reinstall failed: ' + e.message, 'err');
      }
    });
  }
```

- [ ] **Step 5: Commit**

```bash
git add glados/webui/static/ui.js glados/webui/plugin_endpoints.py
git commit -m "feat(webui): plugin modal Logs + About tabs

Logs tab: 100/500/2000 lines selector, Refresh, 5 s auto-refresh
toggle. Renders stdio tail + per-plugin event ring with timestamp +
kind. Auto-refresh tears down on modal close / tab switch.

About tab: name, version, category, persona role, repository URL
(linked), 'Installed from' source URL, Reinstall-from-source button
(confirm → DELETE + POST install with saved URL — config values
intentionally lost on reinstall, that's the upgrade path until v2
brings semver-aware preserves).

Install flow now stashes the source URL in
_meta['com.synssins.glados/source_url'] so About can surface it."
```

---

## Task 10: Add by URL card

**Goal:** A card on the panel with two inputs (URL + slug, slug auto-fills from URL on blur) and an Install button. Success → opens the configuration modal pre-loaded with the new plugin so the operator can fill in values immediately.

**Files:**
- Modify: `glados/webui/static/ui.js` (replace `renderAddByUrlCard` + `wireAddByUrlHandlers` stubs)

**Acceptance Criteria:**
- [ ] Card titled "Add by URL". URL input (full-width) + Slug input (narrow, optional) + Install button.
- [ ] On URL blur, if slug is empty: take last `/` segment, slugify locally, populate.
- [ ] Install button calls POST /api/plugins/install. Disabled while in flight.
- [ ] On success: toast, refresh installed list, open config modal for the new plugin.
- [ ] On 4xx/5xx: inline error below the form.

**Verify:** Manual UI test on deploy.

**Steps:**

- [ ] **Step 1: Replace stubs**

```javascript
function renderAddByUrlCard() {
  return '' +
    '<div class="card" style="margin-top:var(--sp-3);">' +
    '  <div class="section-title">Add by URL</div>' +
    '  <div class="mode-desc" style="margin-bottom:10px;">' +
    '    Paste an MCP <code>server.json</code> URL (https) and optionally a slug. ' +
    '    Slug defaults to the manifest name slugified.' +
    '  </div>' +
    '  <div class="add-url-form" style="display:grid;grid-template-columns:1fr 200px auto;gap:10px;">' +
    '    <input type="url" data-role="install-url" placeholder="https://example.test/server.json">' +
    '    <input type="text" data-role="install-slug" placeholder="optional slug">' +
    '    <button class="btn-primary" data-role="install-btn">Install</button>' +
    '  </div>' +
    '  <div data-role="install-result" class="install-result" style="margin-top:8px;"></div>' +
    '</div>';
}

function wireAddByUrlHandlers() {
  const urlEl = document.querySelector('[data-role="install-url"]');
  const slugEl = document.querySelector('[data-role="install-slug"]');
  const btnEl = document.querySelector('[data-role="install-btn"]');
  const resultEl = document.querySelector('[data-role="install-result"]');
  if (!urlEl || !slugEl || !btnEl) return;

  urlEl.addEventListener('blur', () => {
    if (slugEl.value) return;
    const v = urlEl.value;
    const last = v.split('/').filter(Boolean).pop() || '';
    const stripped = last.replace(/\.json$/i, '').toLowerCase();
    slugEl.value = stripped.replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  });

  btnEl.addEventListener('click', async () => {
    const url = (urlEl.value || '').trim();
    const slug = (slugEl.value || '').trim() || undefined;
    if (!url) { resultEl.textContent = 'URL required'; return; }
    btnEl.disabled = true;
    resultEl.textContent = 'Installing…';
    resultEl.style.color = '';
    try {
      const r = await fetch('/api/plugins/install', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(slug ? { url, slug } : { url }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
      resultEl.textContent = '';
      urlEl.value = ''; slugEl.value = '';
      toast('Plugin installed: ' + data.slug, 'ok');
      await loadPluginsPanel();
      // Auto-open the modal so operator fills in values + enables.
      openPluginConfigModal(data.slug);
    } catch (e) {
      resultEl.textContent = 'Install failed: ' + e.message;
      resultEl.style.color = 'var(--danger)';
    } finally {
      btnEl.disabled = false;
    }
  });
}
```

- [ ] **Step 2: Commit**

```bash
git add glados/webui/static/ui.js
git commit -m "feat(webui): plugins Add-by-URL card

Two inputs (URL + slug) + Install button. Slug auto-fills from URL on
blur. POST /api/plugins/install on click; success opens the config
modal so the operator can immediately fill values + enable. Inline
error on 4xx/5xx."
```

---

## Task 11: Browse card

**Goal:** Card with a collapsible "Index URLs" management section + Browse button → fetches `/api/plugins/browse`, renders a gallery of installable plugins with per-card Install buttons.

**Files:**
- Modify: `glados/webui/static/ui.js` (replace `renderBrowseCard` + `wireBrowseHandlers` stubs)

**Acceptance Criteria:**
- [ ] Card titled "Browse plugins". Index URLs collapsible: list of configured URLs with per-row delete + Add URL input + Save button.
- [ ] Below the URLs section: a Browse button. Click → fetch `/api/plugins/browse`, render gallery.
- [ ] Each gallery card: title, category badge, description, source-index footer, Install button.
- [ ] Install button → POST /api/plugins/install with the entry's `server_json_url` + slugified name → opens config modal on success.
- [ ] Empty state: "No index URLs configured. Add one to browse plugins."

**Verify:** Manual UI test on deploy.

**Steps:**

- [ ] **Step 1: Replace stubs**

```javascript
function renderBrowseCard() {
  return '' +
    '<div class="card" style="margin-top:var(--sp-3);">' +
    '  <div class="section-title">Browse plugins</div>' +
    '  <div class="mode-desc" style="margin-bottom:10px;">' +
    '    Operator-managed list of <code>index.json</code> URLs. The Browse button ' +
    '    merges all configured indexes into a catalog you can install from.' +
    '  </div>' +
    '  <details class="indexes-section">' +
    '    <summary>Index URLs</summary>' +
    '    <div data-role="indexes-list" style="margin-top:8px;"></div>' +
    '    <div style="display:flex;gap:8px;margin-top:8px;">' +
    '      <input type="url" data-role="index-add" placeholder="https://example.test/index.json" style="flex:1;">' +
    '      <button class="btn-secondary" data-role="index-add-btn">Add</button>' +
    '    </div>' +
    '    <div data-role="indexes-result" style="margin-top:6px;font-size:0.8rem;"></div>' +
    '  </details>' +
    '  <div style="margin-top:14px;">' +
    '    <button class="btn-primary" data-role="browse-btn">Browse</button>' +
    '  </div>' +
    '  <div data-role="browse-gallery" class="browse-gallery" style="margin-top:14px;"></div>' +
    '</div>';
}

async function wireBrowseHandlers() {
  const listEl = document.querySelector('[data-role="indexes-list"]');
  const addInput = document.querySelector('[data-role="index-add"]');
  const addBtn = document.querySelector('[data-role="index-add-btn"]');
  const browseBtn = document.querySelector('[data-role="browse-btn"]');
  const gallery = document.querySelector('[data-role="browse-gallery"]');
  if (!listEl || !browseBtn) return;

  let urls = [];

  async function refreshIndexes() {
    try {
      const r = await fetch('/api/plugins/indexes', { credentials: 'same-origin' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      urls = (await r.json()).urls || [];
    } catch (e) {
      listEl.innerHTML = '<div class="mode-desc">Failed to load: ' + e.message + '</div>';
      return;
    }
    if (!urls.length) {
      listEl.innerHTML = '<div class="mode-desc" style="font-style:italic;">No index URLs configured.</div>';
      return;
    }
    listEl.innerHTML = urls.map((u, i) =>
      '<div class="index-row" style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;">' +
      '  <code style="font-size:0.85rem;">' + escapeHtml(u) + '</code>' +
      '  <button class="icon-btn icon-btn-danger" data-index-idx="' + i + '">×</button>' +
      '</div>'
    ).join('');
    listEl.querySelectorAll('button[data-index-idx]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const idx = parseInt(btn.getAttribute('data-index-idx'), 10);
        const next = urls.slice(0, idx).concat(urls.slice(idx + 1));
        await saveIndexes(next);
      });
    });
  }

  async function saveIndexes(newUrls) {
    const resultEl = document.querySelector('[data-role="indexes-result"]');
    resultEl.textContent = 'Saving…';
    try {
      const r = await fetch('/api/plugins/indexes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ urls: newUrls }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
      urls = data.urls || newUrls;
      resultEl.textContent = '';
      await refreshIndexes();
    } catch (e) {
      resultEl.textContent = 'Save failed: ' + e.message;
      resultEl.style.color = 'var(--danger)';
    }
  }

  addBtn.addEventListener('click', async () => {
    const v = (addInput.value || '').trim();
    if (!v) return;
    if (!v.toLowerCase().startsWith('https://')) {
      const resultEl = document.querySelector('[data-role="indexes-result"]');
      resultEl.textContent = 'URL must use https://'; resultEl.style.color = 'var(--danger)';
      return;
    }
    addInput.value = '';
    await saveIndexes(urls.concat([v]));
  });

  browseBtn.addEventListener('click', async () => {
    gallery.innerHTML = '<div class="mode-desc">Loading catalog…</div>';
    try {
      const r = await fetch('/api/plugins/browse', { credentials: 'same-origin' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      renderBrowseGallery(gallery, data);
    } catch (e) {
      gallery.innerHTML = '<div class="mode-desc">Browse failed: ' + e.message + '</div>';
    }
  });

  await refreshIndexes();
}

function renderBrowseGallery(host, data) {
  if (!data.entries || !data.entries.length) {
    host.innerHTML = '<div class="mode-desc">No plugins found in any configured index.</div>';
    return;
  }
  let h = '';
  if (data.errors && data.errors.length) {
    h += '<div class="mode-desc" style="color:var(--danger);margin-bottom:8px;">' +
         'Some indexes failed: ' +
         escapeHtml(data.errors.map(e => e.url + ' — ' + e.error).join('; ')) +
         '</div>';
  }
  h += '<div class="browse-grid">';
  for (const e of data.entries) {
    h += '<div class="browse-card">';
    h += '  <div class="browse-title"><strong>' + escapeHtml(e.title) + '</strong>' +
         ' <span class="plugin-cat-badge">' + escapeHtml(e.category) + '</span></div>';
    if (e.description) {
      h += '  <div class="browse-desc">' + escapeHtml(e.description) + '</div>';
    }
    h += '  <div class="browse-source">from ' + escapeHtml(e.source_index) + '</div>';
    h += '  <button class="btn-primary" data-server-json-url="' + escapeHtml(e.server_json_url) +
         '" data-name="' + escapeHtml(e.name) + '">Install</button>';
    h += '</div>';
  }
  h += '</div>';
  host.innerHTML = h;

  host.querySelectorAll('button[data-server-json-url]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const url = btn.getAttribute('data-server-json-url');
      const name = btn.getAttribute('data-name');
      btn.disabled = true; btn.textContent = 'Installing…';
      try {
        const slugSeed = name.split('/').pop().toLowerCase().replace(/[^a-z0-9]+/g, '-');
        const r = await fetch('/api/plugins/install', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ url: url, slug: slugSeed }),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
        toast('Installed: ' + data.slug, 'ok');
        await loadPluginsPanel();
        openPluginConfigModal(data.slug);
      } catch (e) {
        toast('Install failed: ' + e.message, 'err');
      } finally {
        btn.disabled = false; btn.textContent = 'Install';
      }
    });
  });
}
```

- [ ] **Step 2: CSS for the gallery**

```css
.browse-gallery .browse-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}
.browse-card {
  border: 1px solid var(--border); border-radius: 6px;
  padding: 12px 14px; background: var(--bg-elev);
  display: flex; flex-direction: column; gap: 8px;
}
.browse-title { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.browse-desc  { font-size: 0.85rem; color: var(--fg-secondary); flex-grow: 1; }
.browse-source { font-size: 0.75rem; color: var(--fg-muted); font-style: italic; }
```

- [ ] **Step 3: Commit**

```bash
git add glados/webui/static/ui.js glados/webui/static/style.css
git commit -m "feat(webui): plugins Browse card + index URL management

Browse card holds a collapsible Index URLs editor (add/remove with
persisted save through /api/plugins/indexes) and a Browse button that
fetches the merged catalog from /api/plugins/browse. Each catalog
entry renders as a card with title, category, description,
source-index footer, and an Install button. Install reuses the same
endpoint as Add-by-URL and opens the config modal on success."
```

---

## Task 12: Docs (architecture + CHANGES + README)

**Goal:** `docs/plugins-architecture.md` Phase 2b status flipped to live; `docs/CHANGES.md` Change 32 entry; README Plugins section adds install + browse flows.

**Files:**
- Modify: `docs/plugins-architecture.md`
- Modify: `docs/CHANGES.md`
- Modify: `README.md`

**Acceptance Criteria:**
- [ ] Architecture doc Phase 2b table row marked live (date stamped).
- [ ] Architecture doc updated to note Browse moved from Phase 3 to Phase 2b.
- [ ] CHANGES Change 32 entry follows Change 31's structure (Goals / What changed / Tests / Files touched).
- [ ] README Plugins section shows the operator-facing install + browse flow with screenshots-in-code (or just text walkthrough since we don't ship images).

**Verify:** `git diff --stat` shows only the three doc files.

**Steps:**

- [ ] **Step 1: Update `docs/plugins-architecture.md`**

In the phasing table, update the Phase 2b row:

```markdown
| 2 (full) | WebUI Plugins panel, `uvx` / `npx` runtime spawn, hot-reload, Browse pulled forward | **2026-04-29 (Change 32)** |
| 3 | Curated `synssins/glados-plugins` repo content + initial seed plugins | Next |
```

Add a "Phase 2b shipped" note under the existing "Hot-reload (Phase 2 follow-up)" section explaining the gear-modal UX, browse flow, and the `GLADOS_PLUGINS_ENABLED` flag.

- [ ] **Step 2: Append Change 32 to `docs/CHANGES.md`**

```markdown
## Change 32 — Plugin system Phase 2b: WebUI panel + stdio spawn + Browse (2026-04-29)

Phase 2a (Change 31) shipped the on-disk format, manifest parser, loader,
and runner. Phase 2b makes plugins useful end-to-end: stdio plugins now
spawn via uvx/npx with per-plugin caches; the WebUI exposes
install/configure/enable-toggle/logs/browse via a gear-icon-modal UX;
operators can register multiple `index.json` URLs and browse catalogs.

**Image (Dockerfile)**

- `pip install uv` brings `uvx` onto PATH (~25 MB).
- NodeSource setup_20.x + `apt-get install nodejs` brings `npx` onto
  PATH (~30 MB).
- `mkdir -p /app/logs/plugins` for stdio stderr capture.

**`GLADOS_PLUGINS_ENABLED` gate (engine.py)**

- Default `true`. When set to `false`/`0`/`no`/`off`, engine logs
  `Plugins disabled by GLADOS_PLUGINS_ENABLED env` and skips
  `discover_plugins`. WebUI panel renders an "off" notice. Read once
  at startup; flipping requires a container restart.

**Runner cache routing (`glados/plugins/runner.py`)**

- uvx packages: `--cache-dir <plugin>/.uvx-cache` injected into args
  immediately after `<pkg>@<ver>`.
- npx packages: `npm_config_cache=<plugin>/.uvx-cache` env injected.
- `.uvx-cache/` lives under `/app/data/plugins/<name>/`, survives
  image rebuilds.

**MCPManager per-plugin lifecycle (`glados/mcp/manager.py`)**

- `add_server(cfg)` schedules `_session_runner` for one plugin and
  registers in `_servers` + `_session_tasks`. Raises `MCPError` on
  duplicate name.
- `remove_server(name)` cancels the task, awaits up to 5 s, drops from
  internal state. No-op if missing.
- Per-plugin event ring (`deque maxlen=256`) records connect /
  disconnect / error / tools events; `get_plugin_events(name, limit)`
  surfaces them to the WebUI Logs tab.
- stdio errlog routes to `/app/logs/plugins/<name>.log` instead of
  DEVNULL. Lazy size-cap rotation (>1 MB → `.log.1`).

**Plugin store helpers (`glados/plugins/store.py`)**

- `install_plugin(plugins_dir, slug, manifest)` — atomic dir create
  via `<slug>.installing/` rename. Stub `runtime.yaml` written
  disabled.
- `remove_plugin(plugins_dir, slug)` — rmtree with `..` safety.
- `set_enabled(plugin_dir, enabled)` — runtime.yaml flip.
- `slugify(name, existing)` — last segment, lowercased,
  non-alphanumeric → `-`, collisions `-2`..`-100`.

**Endpoint surface (`glados/webui/tts_ui.py` + `plugin_endpoints.py`)**

11 new endpoints under `/api/plugins/*`, all admin-only:

- `GET /api/plugins`, `GET /api/plugins/<slug>`,
  `POST /api/plugins/install`, `POST /api/plugins/<slug>`,
  `POST /api/plugins/<slug>/enable`, `POST /api/plugins/<slug>/disable`,
  `DELETE /api/plugins/<slug>`, `GET /api/plugins/<slug>/logs`,
  `GET /api/plugins/indexes`, `POST /api/plugins/indexes`,
  `GET /api/plugins/browse`.

Install flow enforces https-only, rejects RFC1918/loopback/link-local
resolutions (SSRF guard), 256 KB manifest cap, 5 s fetch timeout. Save
runtime supports a `***` sentinel: secrets unchanged at the client
preserve via the server-side merge.

**WebUI panel (`glados/webui/static/ui.js`)**

- Three cards under System → Services: Installed plugins (per-row
  layout `[icon] name vX.Y.Z [cat] ●  [⏻ toggle]  [⚙]  [🗑]`),
  Add-by-URL, Browse.
- Gear icon opens a centered modal with three tabs:
  Configuration / Logs / About. Configuration auto-renders from
  `server.json` (env vars / headers / arguments) with typed inputs:
  password for secrets, select for choices, url for format=url,
  required-asterisk for isRequired, default → placeholder.
- Logs tab: 100/500/2000 lines, Refresh, 5 s auto-refresh, both
  stdio tail + event ring.
- About tab: name, version, category, persona role, repository,
  source index, Reinstall-from-source button.
- Browse card: collapsible Index URLs editor + Browse button → gallery.
- Polls `/api/plugins` every 30 s while System tab is visible.

**`ServicesConfig.plugin_indexes`** — new `list[str]` field on
`services.yaml`. https-only validator at load time. Default empty.

**Tests**: +35-45 across `tests/test_engine_plugin_gate.py`,
`tests/test_plugins_runner.py`, `tests/test_mcp_manager_lifecycle.py`,
`tests/test_plugins_store.py`, `tests/test_webui_plugins.py`,
`tests/test_services_config_plugin_indexes.py`.

Suite: 1519 → ~1560.

**Files touched**: see commit list under Phase 2b. Architecture doc
flipped to live; Phase 3 now ships only the curated repo *content*
(the *consumer* — Browse — is in Phase 2b).
```

- [ ] **Step 3: Update README Plugins section**

In `README.md`, find the existing Plugins section (added in Change 31). Append:

```markdown
## Plugins — Phase 2b operator guide

GLaDOS plugins are MCP servers that conform to the standard
`server.json` manifest. Plugins live under `/app/data/plugins/<slug>/`
and survive image rebuilds.

### Enabling plugins

Set `GLADOS_PLUGINS_ENABLED=true` (default) in your `docker-compose.yml`
service env. To neutralize the runtime entirely, set it to `false` and
restart the container.

### Installing a plugin

1. Navigate to **System → Services**, scroll to the **Plugins** card.
2. Use **Add by URL**: paste a `server.json` URL, optionally edit the
   slug, click Install. The configuration modal opens automatically.
3. Fill in the configuration values (env vars, headers, secrets).
   Secrets are masked on subsequent reads.
4. Save, then toggle the Enabled switch. The plugin's tools become
   available to the LLM immediately — no container restart.

### Browsing plugins

1. Add one or more `index.json` URLs to the **Browse** card's
   Index URLs section. (The curated `synssins/glados-plugins` repo
   ships in a future release; for now, point at any compliant index.)
2. Click Browse. Each catalog entry has an Install button that
   pre-populates the URL.

### Per-plugin logs

Click the gear icon on any installed plugin → Logs tab. Shows the
plugin subprocess's stderr (rotated at 1 MB, one backup) plus
connect/disconnect/tool-refresh/error events. Auto-refresh available.
```

- [ ] **Step 4: Commit**

```bash
git add docs/plugins-architecture.md docs/CHANGES.md README.md
git commit -m "docs: Phase 2b shipped — architecture doc + CHANGES 32 + README install flow

Architecture doc Phase 2b row flipped to live; Browse pulled forward
from Phase 3 noted. Change 32 entry covers the full delta (Dockerfile,
gate flag, runner cache, manager lifecycle, store helpers, 11
endpoints, WebUI panel + modal, services.yaml plugin_indexes field,
test budget). README adds the operator-facing install + browse +
logs walkthrough."
```

---

## Task 13: Deploy + smoke test

**Goal:** Build + deploy the worktree to the operator's Docker host, manually verify the four key flows: install + enable a remote plugin, install + enable a stdio plugin, browse from a configured index URL, log capture.

**Files:** none — this is a deploy task.

**Acceptance Criteria:**
- [ ] `scripts/_local_deploy.py` succeeds, container reports healthy on `/health` (8015 + 8052).
- [ ] `docker exec <container> which uvx` returns a path; same for `which npx`.
- [ ] Manual smoke: install a known-good remote plugin (e.g. HA mcp_server with op's HA token), enable, verify tools appear in MCPManager via `/api/mcp/status`.
- [ ] Manual smoke: install `@thelord/mcp-arr` via Add-by-URL with a stub `server.json` (operator-supplied), enable, verify subprocess appears in `docker exec <container> ps -ef | grep mcp-arr`.
- [ ] Manual smoke: configure `GLADOS_PLUGINS_ENABLED=false`, restart, verify panel shows off-state.
- [ ] No regressions: existing chat / TTS / STT / HA Tier 1 + Tier 3 flows unchanged.

**Verify:** Operator confirms in-WebUI manual checks pass.

**Steps:**

- [ ] **Step 1: Pre-flight — full test suite**

```bash
cd C:/src/glados-container/.worktrees/webui-polish
python -m pytest -q
```
Expected: ~1560 passed.

- [ ] **Step 2: Run local deploy**

```bash
cd C:/src/glados-container/.worktrees/webui-polish
MSYS_NO_PATHCONV=1 \
GLADOS_SSH_HOST=<from SESSION_STATE Credentials> \
GLADOS_SSH_USER=<...> \
GLADOS_SSH_PASSWORD=<...> \
GLADOS_COMPOSE_PATH=<...> \
python scripts/_local_deploy.py
```

Expected: SSH connection, image build on the docker host, container recreate, /health green on 8015 + 8052.

- [ ] **Step 3: Confirm uvx + npx land in image**

```bash
ssh <ssh-host> "docker exec <container> which uvx && docker exec <container> which npx"
```
Expected: two paths printed.

- [ ] **Step 4: Confirm log dir exists**

```bash
ssh <ssh-host> "docker exec <container> ls -la /app/logs/plugins"
```
Expected: directory exists, owned by glados user.

- [ ] **Step 5: Manual remote-plugin smoke**

In WebUI: System → Services → Plugins → Add by URL → install a remote
manifest (e.g. an HA `mcp_server` server.json — the operator can
hand-roll a minimal one pointing at the op's HA URL with the token as
a remote header). Toggle Enabled. Verify in System → Services that the
plugin's tools appear in `/api/mcp/status` (or whatever the existing
status panel surfaces).

- [ ] **Step 6: Manual stdio-plugin smoke**

Pick one stdio MCP server with a real upstream `server.json` (e.g.
the operator's `@thelord/mcp-arr` if one exists, or hand-roll a
minimal one for `@modelcontextprotocol/server-everything`). Install,
configure, enable. Verify subprocess starts:

```bash
ssh <ssh-host> "docker exec <container> ps -ef | grep -E '(uvx|npx|mcp)'"
```

- [ ] **Step 7: Manual off-state smoke**

In compose, set `GLADOS_PLUGINS_ENABLED=false`. `docker compose up -d`.
Verify WebUI panel renders the off-notice and no plugin subprocesses
are running.

- [ ] **Step 8: Update SESSION_STATE.md "Active Handoff"**

Replace top section with the post-Phase-2b state:

```markdown
## 🟢 Active Handoff — 2026-04-30 (Phase 2b shipped)

**Session scope (2026-04-30):** Phase 2b of the plugin system landed
end-to-end. Image now ships uvx + Node 20 (gated by
`GLADOS_PLUGINS_ENABLED`); WebUI exposes install / browse / configure
/ enable-toggle / logs via a gear-modal UX; per-plugin event ring +
log file rotation. See CHANGES.md Change 32.

**Live state:**
- Branch `webui-polish`, HEAD = post-Phase-2b deploy commit. Merge to
  `main` next session after a soak.
- Container image SHA <NEW_SHA> running on glados.denofsyn.com
  (post-Change-32 deploy, healthy).
- Test suite: ~1560 / 5 / 0.

**Open priorities (top to bottom):**
1. Phase 1 — wire HA `mcp_server` as the first cataloged plugin.
2. Phase 3 — populate `synssins/glados-plugins` curated repo with
   initial seed (mcp-arr, mcp-spotify, mcp-tautulli, mcp-github,
   mcp-fetch).
3. (carried) %s/%d log placeholder sweep.
4. (carried) Drop "good morning" from looks_like_home_command activity
   phrases.
... (rest of prior priorities)
```

- [ ] **Step 9: Commit SESSION_STATE update**

```bash
cd C:/src
git add SESSION_STATE.md
git commit -m "session-state: Phase 2b plugin system shipped + image SHA updated"
```

(Note: SESSION_STATE.md is in the parent dir, not the worktree — git status is separate.)

- [ ] **Step 10: Optional `webui-polish` → `main` merge after soak**

Defer to operator approval — they may want to soak Phase 2b for a day before merging trunk.

---

## Self-Review Checklist (run after writing the plan)

- **Spec coverage:** Browse — T5+T6+T11. Gear modal — T8+T9. Hot-rotate via toggle — T2+T7. Off-state — T0+T7. Per-plugin logs — T2+T9. Install-from-URL — T4+T10. ✓
- **No placeholders:** searched for "TBD" / "TODO" / "fill in" — none. ✓
- **Type consistency:** `MCPServerConfig`, `Plugin`, `RuntimeConfig`, `ServerJSON` named consistently across tasks. `add_server` / `remove_server` / `_record_event` / `get_plugin_events` consistent. ✓
- **`engine.mcp_manager` access:** assumed reachable via `_aw._engine.mcp_manager` per spec — confirmed in code (engine.py:720). ✓
- **`_send_json`, `_read_json_body`, `_put_config_section`** — assumed to exist in tts_ui.py per existing code patterns. Verify on first endpoint task; fall back to inline if missing.

---

## Open after this plan

- Phase 1 (HA `mcp_server` as first cataloged plugin) is the natural next workstream.
- Phase 3 (curated catalog content) becomes mostly a content-creation effort: the consumer is in this plan.
- If a future plugin needs `dnx` (.NET) runtime, add to `_RUNTIME_COMMANDS` in `runner.py` + Dockerfile dotnet install.
