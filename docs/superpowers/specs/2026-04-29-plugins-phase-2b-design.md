# Plugin system — Phase 2b design (2026-04-29)

**Status:** Spec for the WebUI panel, runtime stdio spawn, and per-plugin
hot-rotate work that follows Phase 2a (Change 31, 2026-04-29).

**Scope:** This phase makes plugins useful end-to-end. Phase 2a shipped
the on-disk format, manifest parser, loader, and `MCPServerConfig`
conversion — but `uvx`/`npx` aren't on the container PATH, the WebUI has
no plugin surface, and there is no way to add/remove a plugin without a
container restart. Phase 2b closes those three gaps.

**Out of scope** (deferred to later phases):

- Curated `synssins/glados-plugins` GitHub repo + "Browse Plugins" tab
  (Phase 3).
- Wiring HA's `mcp_server` integration as the first cataloged plugin
  (Phase 1, separate workstream).
- GLaDOS-as-MCP-server endpoint on port 8017 (Phase 4).
- Sidecar/external escape hatch (Phase 5).

Reference: [`docs/plugins-architecture.md`](../../plugins-architecture.md)
holds the canonical design (storage layout, `_meta` namespace, runtime
mapping, trust posture, phasing). This spec is implementation detail
on top of that.

---

## Goals

1. Operator can install a plugin by pasting a `server.json` URL into the
   WebUI, fill in env/header/secret values via an auto-rendered form,
   and bring it online without a container restart.
2. Operator can enable/disable a plugin via a per-plugin toggle that
   hot-rotates that one plugin's MCP session — other plugins stay
   connected.
3. Operator can read each plugin's stdout/stderr (stdio plugins) or
   connect/disconnect/error events (remote plugins) via a per-plugin
   logs view.
4. Stdio plugins run via `uvx` (Python) or `npx` (Node), with per-plugin
   caches under `/app/data/plugins/<name>/.uvx-cache/` so caches survive
   image rebuilds.
5. The whole subsystem is gated by a single compose-level switch
   (`GLADOS_PLUGINS_ENABLED`). When off, no discovery, no spawn, WebUI
   panel renders an "off" notice.

## Locked decisions (from 2026-04-29 brainstorm)

| # | Decision |
|---|---|
| Q1 | Single image ships uvx + Node; runtime gated by `GLADOS_PLUGINS_ENABLED` env (default `true`). |
| Q2 | Install flow accepts both file-drop and paste-URL. Phase 3's curated browser will reuse the URL path. |
| Q3 | Per-plugin enable toggle hot-rotates that plugin's session. No global "Reload all" button. Per-plugin logs surfaced in the WebUI. |
| Q4 | `MCPManager.add_server(config)` / `remove_server(name)` — per-plugin task lifecycle, not bulk swap. |
| Q5 | Plugin directory name = slugified last segment of `server.json.name`, operator-editable in the install form. Collisions append `-2`, `-3`, … |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  WebUI (System → Services → Plugins sub-panel)                   │
│  ─────────────────────────────────────────────────────────────   │
│  Installed list ─┬─► toggle enabled ─► POST /api/plugins/<n>/    │
│                  │                       enable|disable          │
│                  ├─► configure  ─────► POST /api/plugins/<n>     │
│                  ├─► logs       ─────► GET  /api/plugins/<n>/    │
│                  │                       logs                    │
│                  └─► remove     ─────► DELETE /api/plugins/<n>   │
│  Add by URL    ──────────────────────► POST /api/plugins/install │
└──────────────────────────────────┬───────────────────────────────┘
                                   │
                ┌──────────────────▼─────────────────────┐
                │  glados.plugins.store                  │
                │   install_plugin / remove_plugin       │
                │   save_runtime / save_secrets          │
                │   set_enabled                          │
                │   write atomic; secrets.env mode 0600  │
                └──────────────────┬─────────────────────┘
                                   │
            ┌──────────────────────▼──────────────────────┐
            │  glados.plugins.runner                      │
            │   plugin_to_mcp_config(plugin)              │
            │   • injects --cache-dir for uvx             │
            │   • injects npm_config_cache env for npx    │
            └──────────────────────┬──────────────────────┘
                                   │
                ┌──────────────────▼─────────────────────┐
                │  glados.mcp.manager.MCPManager         │
                │   add_server(cfg) / remove_server(n)   │
                │   per-plugin errlog → /app/logs/       │
                │     plugins/<name>.log                 │
                │   in-memory ring of                    │
                │     connect/disconnect/error events    │
                └────────────────────────────────────────┘
```

### Component responsibilities

- **`glados.plugins.store`** owns the `/app/data/plugins/<name>/` filesystem.
  All atomic writes go through here. Phase 2b adds three new functions:
  `install_plugin`, `remove_plugin`, `set_enabled`.
- **`glados.plugins.runner`** translates a `Plugin` to an
  `MCPServerConfig`. Phase 2b extends `_build_local_config` to inject
  `--cache-dir <plugin_dir>/.uvx-cache` for uvx and
  `npm_config_cache=<plugin_dir>/.uvx-cache` env for npx.
- **`glados.mcp.manager.MCPManager`** handles per-plugin task lifecycle.
  Phase 2b adds `add_server` (sync wrapper around an async `_start_session`),
  `remove_server` (cancel + drain the task, drop from `_servers`), and
  per-plugin event ring + log file routing.
- **WebUI handlers in `tts_ui.py`** are the HTTP surface. Same auth
  posture as System → Services (admin only).
- **Engine wire-in (`core/engine.py`)** gates plugin discovery on
  `GLADOS_PLUGINS_ENABLED`. The flag is read once at startup; flipping
  it requires a container restart (documented).

---

## Data flow

### Install by URL

1. Operator pastes `https://.../server.json` into the install form,
   optionally edits the slug.
2. WebUI POSTs `/api/plugins/install` with `{url, slug}`.
3. Handler validates `url` (https only, max 256 KB response, 5 s timeout),
   fetches the manifest, validates against `ServerJSON`, slugifies if
   slug is empty.
4. Handler resolves slug collisions by appending `-2`, `-3`, ….
5. `store.install_plugin(slug, manifest)` writes
   `/app/data/plugins/<slug>/server.json` and a stub
   `runtime.yaml` (`enabled: false`, `package_index: 0` or
   `remote_index: 0`, empty value maps).
6. Response includes the slug + the manifest for the WebUI to render
   the configuration form.

### Configure

1. Operator fills the auto-rendered form (env vars, remote headers,
   package args). Secrets show masked.
2. WebUI POSTs `/api/plugins/<slug>` with `{env_values, header_values,
   arg_values, secrets}`.
3. Handler updates `runtime.yaml` (non-secrets) and `secrets.env`
   (secrets, mode 0600). Plugin stays in current enabled state — saving
   does NOT auto-enable.

### Enable / disable (hot-rotate)

1. Operator toggles the per-plugin Enabled switch.
2. WebUI POSTs `/api/plugins/<slug>/enable` (or `/disable`).
3. Handler:
   - Updates `runtime.yaml.enabled` via `store.set_enabled`.
   - On enable: `load_plugin → plugin_to_mcp_config → MCPManager.add_server(cfg)`.
   - On disable: `MCPManager.remove_server(slug)`.
4. Other plugins' sessions are untouched.

### Remove

1. WebUI DELETE `/api/plugins/<slug>`.
2. Handler calls `MCPManager.remove_server(slug)` if running, then
   `store.remove_plugin(slug)` (rmtree).

### Logs

1. WebUI GET `/api/plugins/<slug>/logs?lines=N` (default 200, max 5000).
2. Handler returns `{stdio_log: [...lines...], events: [...]}`.
   - `stdio_log` is the tail of `/app/logs/plugins/<slug>.log` (stdio
     plugins only; empty array for remote).
   - `events` is the ring buffer for that plugin (connect/disconnect/
     tool-refresh/error events).

---

## File-level changes

### `Dockerfile`

- After the existing `pip install` line: add `pip install --no-cache-dir uv`
  (~25 MB, brings `uvx` onto PATH).
- Add Node 20 via NodeSource + apt:
  ```
  RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
      && apt-get install -y --no-install-recommends nodejs \
      && rm -rf /var/lib/apt/lists/*
  ```
  (~30 MB). Brings `npx` onto PATH.
- Add `mkdir -p /app/logs/plugins` to the existing `mkdir` line so the
  log directory exists before the container starts.

### `glados/plugins/runner.py`

- `_build_stdio_args(package, plugin)` already takes `plugin: Plugin`
  (so it has access to `plugin.directory`). Insert the cache flag for
  uvx as the first arg AFTER the package identifier:
  ```python
  if package.runtime_hint == "uvx":
      args.extend(["--cache-dir", str(plugin.directory / ".uvx-cache")])
  ```
- `_resolve_env(package, plugin)` already returns the env dict. For
  npx, append `npm_config_cache=<plugin.directory>/.uvx-cache` to the
  resolved env. (uvx uses the CLI flag; npx uses the env var.)
- Plugin directory's `.uvx-cache/` is created on first spawn by uvx/npx
  themselves; no need to mkdir up front.

### `glados/plugins/store.py`

Three new functions:

```python
def install_plugin(plugins_dir: Path, slug: str, manifest: ServerJSON,
                   stub_runtime: RuntimeConfig | None = None) -> Path:
    """Create /app/data/plugins/<slug>/ with server.json + a stub
    runtime.yaml. Atomic — writes to <slug>.installing/, fsyncs, renames
    to <slug>/. Raises InstallError if <slug>/ already exists."""

def remove_plugin(plugins_dir: Path, slug: str) -> None:
    """rmtree of /app/data/plugins/<slug>/. No-op if missing.
    Refuses to delete anything outside plugins_dir."""

def set_enabled(plugin_dir: Path, enabled: bool) -> RuntimeConfig:
    """Read runtime.yaml, flip enabled, save_runtime. Returns the new
    RuntimeConfig."""
```

Plus a small helper:

```python
def slugify(name: str, existing: set[str]) -> str:
    """Last path segment lowercased, non-alphanumeric → '-', collisions
    suffix '-2', '-3', ..."""
```

### `glados/mcp/manager.py`

- Extract the body of `_session_runner` into a method that takes a
  single `MCPServerConfig` and runs one session loop — the existing
  shape already does this; just promote it.
- Add per-plugin event ring + accessor:
  ```python
  self._plugin_events: dict[str, deque[dict]] = defaultdict(
      lambda: deque(maxlen=256)
  )
  def get_plugin_events(self, name: str) -> list[dict]: ...
  ```
  Every existing `self._observability_bus.emit(...)` call site that has
  a `config.name` also appends to `self._plugin_events[config.name]`.
- For stdio plugins, replace `errlog=subprocess.DEVNULL` with a
  per-plugin file handle:
  ```python
  log_path = Path("/app/logs/plugins") / f"{config.name}.log"
  log_fd = open(log_path, "ab", buffering=0)  # rotated lazily
  async with stdio_client(params, errlog=log_fd) as streams:
      ...
  ```
  Lazy rotation: before opening, if file > 1 MB, rename to `.log.1`
  (replacing any existing `.log.1`). One backup, simple, no scheduler.
- Add new public methods:
  ```python
  def add_server(self, config: MCPServerConfig) -> None:
      """Thread-safe. Schedules _session_runner(config) on the loop,
      registers in self._servers and self._session_tasks. Raises if
      a server with this name is already running."""

  def remove_server(self, name: str) -> None:
      """Thread-safe. Cancels self._session_tasks[name], waits up to
      5 s for cleanup, drops from _servers. No-op if missing."""
  ```
  Both invoke via `asyncio.run_coroutine_threadsafe(_, self._loop)` so
  HTTP handlers can call them synchronously.

### `glados/core/engine.py`

- Wrap the existing `discover_plugins` block with a feature flag:
  ```python
  if os.environ.get("GLADOS_PLUGINS_ENABLED", "true").lower() in ("1","true","yes","on"):
      try:
          from glados.plugins import discover_plugins, plugin_to_mcp_config
          ...
  else:
      logger.info("Plugins disabled by GLADOS_PLUGINS_ENABLED env")
  ```
- WebUI handlers reach the `MCPManager` via the existing engine
  reference: `_aw._engine.mcp_manager` (already used in tts_ui.py for
  memory-store + personality-preprompt access; engine.py:720 declares
  `self.mcp_manager`). No new wiring required.

### `glados/webui/tts_ui.py`

New endpoints (all admin-only):

| Method | Path | Purpose |
|---|---|---|
| GET    | `/api/plugins`                         | List all plugins (slug, name, version, enabled, status, category, icon). |
| GET    | `/api/plugins/<slug>`                  | Full manifest + runtime + non-secret values. Secrets returned as `"***"` placeholders. |
| POST   | `/api/plugins/install`                 | Body `{url, slug?}`. Fetches + validates manifest, writes stub runtime, returns `{slug, manifest}`. |
| POST   | `/api/plugins/<slug>`                  | Save runtime config (env_values, header_values, arg_values, secrets). |
| POST   | `/api/plugins/<slug>/enable`           | Flip enabled=true, hot-add session. |
| POST   | `/api/plugins/<slug>/disable`          | Flip enabled=false, hot-remove session. |
| DELETE | `/api/plugins/<slug>`                  | Stop + delete. |
| GET    | `/api/plugins/<slug>/logs?lines=N`     | `{stdio_log: [...], events: [...]}`. |

Constraints on `/api/plugins/install`:
- URL must be `https://`. Reject `http://` and other schemes.
- Manifest fetch capped at 256 KB / 5 s timeout. SSRF mitigation: refuse
  URLs that resolve to RFC1918 / loopback / link-local addresses.
- Slug regex: `^[a-z0-9][a-z0-9-]{0,62}$`. Reject anything else.

### `glados/webui/static/ui.js`

- New sub-section inside the System → Services tab, beneath the LLM
  Endpoints card. Title: "Plugins". Renders empty-state when
  `GLADOS_PLUGINS_ENABLED=false` (server returns a flag in
  `GET /api/plugins`).
- Per-plugin row: name + version + category icon, an Enabled toggle,
  expand button. Expanded row shows the auto-generated form (env vars,
  remote headers, package arg overrides), Save button, Logs tab.
- Form rendering rules from `server.json`:
  - `isSecret: true` → `<input type="password">`, server returns `"***"`,
    only POST when the operator changes it (treat unchanged ones as
    "leave as-is").
  - `choices: [...]` → `<select>`.
  - `format: "url"` → `<input type="url">`, validate on blur.
  - `isRequired: true` → red asterisk + client-side required.
  - `default` → placeholder text only; not auto-filled (so empty stays
    empty; manifest default applies at runtime via `runner._resolve_env`).
- "Add plugin" card: URL input + slug input (auto-fills from URL on
  blur) + Install button.

---

## Error handling

- **Manifest fetch failure (404, timeout, oversize):** install endpoint
  returns 400 with the upstream error message. WebUI shows it inline.
- **Manifest schema validation failure:** install endpoint returns 400
  with the Pydantic error details, capped to 1 KB so a malicious
  manifest can't blow up the response.
- **Slug collision:** install endpoint auto-appends `-2`, `-3`, …; if
  100 collisions, return 409. (No realistic operator hits this.)
- **Enable failure (uvx not found, package missing, network error):**
  `MCPManager.add_server` succeeds (the task is scheduled), but the
  session loop's existing retry-with-backoff catches the spawn failure,
  emits an error event to the ring buffer, and retries every 2 s.
  WebUI shows the latest error in the logs view. Operator can disable
  to stop the retry loop.
- **Remove with active session:** `remove_server` cancels the task,
  awaits up to 5 s. After 5 s, gives up but still drops from
  `_servers` (the orphaned task self-destructs eventually). Logged at
  warning level.
- **`GLADOS_PLUGINS_ENABLED=false` runtime flip:** Engine reads the
  env once at startup. Flipping it after start has no effect; documented
  in the WebUI panel's "off" notice ("Restart the container after
  changing this").

---

## Testing

Target +25–35 tests (suite 1519 → ~1550). Test files:

- `tests/test_plugins_runner.py` (new) — cache flag + env injected
  correctly for uvx and npx. Verify `--cache-dir <plugin>/.uvx-cache`
  appears in args for uvx; `npm_config_cache` lands in env for npx.
- `tests/test_plugins_store.py` (new) — `install_plugin` writes the
  expected files atomically; `remove_plugin` rmtree-s; `set_enabled`
  round-trips; `slugify` collision behavior.
- `tests/test_mcp_manager.py` (extend) — `add_server` /
  `remove_server` lifecycle. Mock `_open_transport` so the test doesn't
  spawn real subprocesses. Verify per-plugin event ring grows and
  caps at 256.
- `tests/test_webui_plugins.py` (new) — handler round-trip for each of
  the 8 endpoints. Mock manifest fetch with a stub HTTP server. Verify
  install URL validation (https-only, SSRF protection, schema
  rejection).
- `tests/test_engine_plugin_gate.py` (new) — `GLADOS_PLUGINS_ENABLED`
  off → discover_plugins not called.

Manual verification (after deploy):

1. Drop a known-good remote plugin (HA `mcp_server`-shaped) into
   `/app/data/plugins/ha-test/` with `enabled: false`. Reload WebUI →
   appears in the list. Toggle Enabled → tools appear in MCPManager.
   Toggle off → tools disappear.
2. Install `@thelord/mcp-arr` via the URL form (paste the GitHub raw
   `server.json`). Configure SONARR_URL/SONARR_API_KEY. Enable.
   Verify uvx/npx spawn (`docker exec ... ps -ef | grep mcp-arr`).
   Verify `/app/logs/plugins/mcp-arr.log` populates.
3. Flip `GLADOS_PLUGINS_ENABLED=false` in compose, restart, confirm
   the WebUI panel renders the "off" notice and no plugin sessions
   are active.

---

## Migration / compatibility

- Phase 2a's filesystem layout is unchanged. Existing
  `/app/data/plugins/<name>/` directories that have valid
  `server.json` + `runtime.yaml` continue to load identically.
- `MCPServerConfig` schema unchanged. Existing YAML-configured
  `mcp_servers` continue to work and now coexist with toggleable
  plugins.
- No DB schema changes. No new compose ports.

---

## Risks

| Risk | Mitigation |
|---|---|
| Node 20 in image enlarges attack surface. | Acceptable for a trust-the-operator container; documented in README plugin section. `GLADOS_PLUGINS_ENABLED=false` neutralises everything. |
| uvx/npx can fetch arbitrary network packages. | Already covered by architecture doc trust posture (matches HA `custom_components`). Curated catalog (Phase 3) pins versions. |
| Per-plugin log files unbounded growth. | 1 MB rotation with one backup. ~2 MB ceiling per plugin. |
| Manifest fetch SSRF. | Reject non-https, reject RFC1918 / loopback resolutions, 256 KB cap. |
| Long-running stdio plugin masks errors that don't reach stderr. | Connect/disconnect/error events go to the in-memory ring regardless. |

---

## Implementation order (deploy-able increments)

Each step is a separate commit and a deployable build.

1. **Step 0 — Image + flag.** Dockerfile uvx + Node 20.
   `GLADOS_PLUGINS_ENABLED` gate in `engine.py`. No behaviour change
   for operators with the flag false (default true). Verify uvx and
   npx are on PATH inside the container.
2. **Step 1 — Runner cache + manager API.** `runner.py` cache flag /
   env. `mcp/manager.py` add_server / remove_server / per-plugin event
   ring + log file errlog. Tests for both. No new endpoints yet.
3. **Step 2 — Store install/remove + endpoint surface.** `store.py`
   `install_plugin` / `remove_plugin` / `set_enabled` / `slugify`.
   Eight new `/api/plugins/*` endpoints in `tts_ui.py`. Tests for
   handlers. Still no UI.
4. **Step 3 — WebUI panel.** Plugins sub-section under System →
   Services. Form-render-from-manifest. Toggle. Logs viewer. Install-
   by-URL card.
5. **Step 4 — Docs + change log.** Architecture doc Phase 2b status
   flipped to live. CHANGES.md Change 32. README Plugin section
   updated with the install flow.

Each step deployable independently; rollback is a single revert.

---

## Open after this spec

- Log rotation policy beyond the simple 1 MB / 1 backup default could
  be configurable. Skipped in v1.
- "Browse plugins" curated catalog (Phase 3) reuses the install-by-URL
  flow internally — no new endpoints needed.
- Plugin auto-update on version change is deferred. Operator manually
  re-runs install with the same slug to overwrite.
