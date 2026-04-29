# Plugin bundle format design (2026-04-29)

**Status:** Spec for the v2 plugin install flow. Replaces the v1 `server.json`-fetch path shipped in Phase 2a/2b earlier today (Changes 31, 32) with a self-contained bundle format that operators upload as a zip.

**Operator review (2026-04-29 evening):** the v1 design exposed too much developer-internal terminology (`slug`, `manifest`, `runtime.yaml`, env-var names) and required upstream MCP authors to publish a `server.json` (most don't). The v2 design lets operators take any MCP server from GitHub — no upstream cooperation required — and wrap it in a zip with a single GLaDOS-side manifest.

---

## Goals

1. **Zero upstream cooperation.** Operator can take any GitHub repo that exposes an MCP server (Node, Python, .NET) and bundle it for GLaDOS without modifying upstream code.
2. **Operator-facing terminology only.** No `slug` / `manifest` / `runtime.yaml` / `secrets.env` / env-var names visible. Operators see "Sonarr API Key" not `SONARR_API_KEY`.
3. **Two install paths only.** Browse (curated catalog) and Upload (drag-drop a zip). No URL-paste anywhere.
4. **Three runtime modes.** Registry-installed (uvx/npx fetches the package), bundled-source (run code from inside the zip), remote (connect via streamable-HTTP).
5. **Backward compat with v1.** Existing `server.json`-based installs keep loading via a fallback path. Phase 2a's loader becomes the second-pass.
6. **Safety.** Zip extraction guards against path traversal, symlinks, oversize, archive bombs.

## Bundle format

`.zip` — universal, browser-native drag-drop, every OS understands it.

### Layout

```
my-plugin.zip
├── plugin.json          REQUIRED — GLaDOS bundle manifest (operator-facing settings)
├── README.md            OPTIONAL — markdown rendered in the plugin's About section
├── icon.svg             OPTIONAL — Lucide-style icon for the tab strip + list
├── server.json          OPTIONAL — official MCP server.json if upstream ships one;
│                        used as a fallback source for settings if plugin.json
│                        omits them. Phase 2a/2b loader path stays available for
│                        bundles that lean entirely on this file.
└── src/                 OPTIONAL — bundled MCP server source (any structure)
    ├── package.json
    ├── pyproject.toml
    └── ...
```

Bundle size cap: **50 MB compressed, 200 MB uncompressed** (typical MCP servers
are ~5-30 MB; 50 MB ceiling protects against archive bombs).

### `plugin.json` schema

```json
{
  "schema_version": 1,
  "name": "Sonarr / Radarr Stack",
  "description": "Control your *arr stack via natural language.",
  "version": "1.2.3",
  "category": "media",
  "icon": "tv",
  "persona_role": "interactive",
  "homepage": "https://github.com/example/mcp-arr",

  "runtime": {
    "mode": "bundled",
    "command": "node",
    "args": ["src/dist/index.js"]
  },

  "settings": [
    {
      "key": "SONARR_URL",
      "label": "Sonarr URL",
      "type": "url",
      "required": true,
      "description": "Base URL of your Sonarr instance, e.g. http://sonarr.lan:8989"
    },
    {
      "key": "SONARR_API_KEY",
      "label": "Sonarr API Key",
      "type": "secret",
      "required": true
    },
    {
      "key": "QUALITY",
      "label": "Default quality profile",
      "type": "select",
      "choices": ["720p", "1080p", "2160p"],
      "default": "1080p"
    }
  ]
}
```

**Required fields:** `schema_version`, `name`, `description`, `version`, `category`, `runtime`.
**Optional fields:** all others.

### `runtime.mode` values

| Mode | Required fields | Behavior |
|---|---|---|
| `registry` | `package` (e.g. `uvx:demo-mcp@1.0.0` or `npx:@org/demo@1.0.0`) | Spawn via uvx/npx fetching from PyPI/npm at runtime. No bundled source needed. |
| `bundled` | `command`, `args` | Spawn `command + args` with cwd set to the unpacked bundle directory. Source must be in the bundle. |
| `remote` | `url` (https), optional `headers` | Connect via streamable-HTTP. No process spawned. |

Per-runtime cache directory routing (uvx `--cache-dir`, npx `npm_config_cache`)
stays as designed in Phase 2a — applied automatically by the runner when
`mode == "registry"`.

### `settings[].type` values

| Type | Form widget | Storage | Notes |
|---|---|---|---|
| `text` | `<input type="text">` | runtime.yaml | Plain string |
| `url` | `<input type="url">` | runtime.yaml | Validates URL on blur |
| `number` | `<input type="number">` | runtime.yaml | Numeric input |
| `boolean` | `<input type="checkbox">` | runtime.yaml | Toggle |
| `select` | `<select>` | runtime.yaml | Requires `choices: [...]` |
| `secret` | `<input type="password">` | secrets.env (mode 0600) | Masked as `***` on read |

The `key` field becomes the env var passed to the subprocess — operators never
see it. The `label` is what shows in the form.

### Server-side sanitization & defaults

- **Internal directory name** (formerly "slug") derived server-side from
  `plugin.json.name`: lowercased, non-alphanumeric → `-`, trimmed,
  collisions append `-2`, `-3`. Never displayed in the UI.
- **Category** displayed title-case via a label map: `media → Media`,
  `home → Home`, `system → System`, `dev → Developer`, `utility → Utility`,
  unknown → title-case the literal string.

## Migration from v1 (`server.json`-only installs)

The Phase 2a loader stays. v1 plugins (those installed via Phase 2b's
Add-by-URL flow) have no `plugin.json` — only a `server.json`. The new loader
checks for `plugin.json` first; if missing, it falls back to the v1 path.

Concretely in `glados/plugins/loader.py:load_plugin(plugin_dir)`:
1. If `plugin_dir/plugin.json` exists → parse as new schema, build `Plugin`.
2. Else if `plugin_dir/server.json` exists → existing v1 path (parse as
   `ServerJSON`, derive a synthetic `plugin.json`-equivalent for the form
   renderer).
3. Else → `ManifestError`.

The synthetic conversion (server.json → plugin.json view) lets the WebUI
render the same form whether the bundle was authored as v1 or v2.

The operator's current container has zero installed plugins — the
empty-state notice has been showing since deploy. So no live migration
work is required. The fallback is for any bundle that ships only
`server.json` going forward.

## Install paths

Two paths only. **Add-by-URL goes away.**

### Browse

Same as v2's existing Browse: operator-configured `index.json` URL list,
catalog merge with last-index-wins dedupe, Install button on each entry.

Catalog entry shape extended to support both v1 and v2 sources:

```json
{
  "name": "mcp-arr",
  "title": "Sonarr / Radarr Stack",
  "category": "media",
  "description": "...",
  "bundle_url": "https://.../mcp-arr-1.2.3.zip"
}
```

`bundle_url` (preferred) → fetch the zip, install via Upload pipeline.
Legacy `server_json_url` (kept for backward compat) → fetch as v1.

### Upload

New tab on the Plugins page. Drag-drop or file picker accepting `.zip`.
Browser uploads via `multipart/form-data` to a new `POST /api/plugins/upload`
endpoint. Server:
1. Validates content-length ≤ 50 MB.
2. Reads zip into memory, validates: well-formed, no symlinks, no path
   traversal, no entry larger than 50 MB compressed / 200 MB uncompressed,
   total uncompressed ≤ 200 MB.
3. Extracts to staging dir.
4. Validates `plugin.json` exists and parses cleanly.
5. Renames staging → `/app/data/plugins/<derived-internal-name>/` atomically.
6. Returns `{name, internal_name, plugin}` so the WebUI can switch to the new
   plugin's tab and pre-fill the form.

## File-level changes

| File | Change |
|---|---|
| `glados/plugins/bundle.py` (new) | `PluginJSON` Pydantic model; `Setting`, `RuntimeMode`; v1→v2 synthetic conversion |
| `glados/plugins/loader.py` | `load_plugin` prefers `plugin.json`, falls back to `server.json` |
| `glados/plugins/runner.py` | runtime mapping for the three modes |
| `glados/plugins/store.py` | `install_from_zip(zip_bytes, plugins_dir) -> Plugin` |
| `glados/webui/plugin_endpoints.py` | new `install_from_zip_bytes` helper; remove `install_from_url` (or keep behind a hidden route for now) |
| `glados/webui/tts_ui.py` | new `POST /api/plugins/upload` handler (multipart) |
| `glados/webui/static/ui.js` | drop Add-by-URL section; add Upload section (drag-drop + file picker); form labels source from `setting.label` not the key |
| `glados/webui/static/style.css` | upload drop-zone styling; existing-page-conformance pass per audit |
| `tests/test_plugins_bundle.py` | new — schema, v1→v2 conversion, runtime modes |
| `tests/test_plugins_zip_install.py` | new — extraction safety (traversal, symlink, oversize), atomic rename |
| `tests/test_webui_plugins.py` | extend with upload endpoint tests |
| `docs/plugin-bundle-format.md` | new — schema reference + "wrap any MCP server in 5 minutes" guide |
| `docs/plugins-architecture.md` | v1→v2 migration note |
| `docs/CHANGES.md` | Change 33 |
| `README.md` | install flow update |

## Zip extraction safety

`install_from_zip` uses Python's `zipfile.ZipFile` with these guards:

```python
def _safe_extract(zf: ZipFile, dest: Path) -> None:
    for member in zf.infolist():
        # No symlinks (Linux external_attr bit 0xA000_0000 in upper 16 of attr).
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise InstallError("zip contains a symlink; refusing")
        # No absolute paths or path traversal.
        target = (dest / member.filename).resolve()
        if not str(target).startswith(str(dest.resolve()) + os.sep) and target != dest.resolve():
            raise InstallError(f"zip member {member.filename!r} escapes target dir")
        # Size cap per entry.
        if member.file_size > 200 * 1024 * 1024:
            raise InstallError(f"zip member {member.filename!r} too large ({member.file_size} bytes)")
    zf.extractall(dest)
```

Plus a total-uncompressed-size cap before extraction starts.

## Operator-facing terminology pass

Every operator-visible string sweep:

| v1 term | v2 term |
|---|---|
| Slug | (removed; never shown) |
| Folder name | (removed; never shown) |
| Manifest | (removed; "Plugin info" or just contextual "this plugin") |
| `runtime.yaml` | (removed; settings just persist) |
| `secrets.env` | (removed; secrets persist privately) |
| `SONARR_API_KEY` | "Sonarr API Key" (from `setting.label`) |
| `runtimeHint` | (removed; mode is internal detail) |

Error messages too: "manifest failed schema validation" → "plugin.json is
malformed: {pydantic error}". Etc.

## Page-level design conformance

The Phase 2b rework (commit `0bf3efe`) introduced custom classes
(`.plugins-page`, `.page-tabs`, `.plugin-header-card`) without grounding in
the existing Configuration sub-page conventions. v2 fixes this:

- Run an audit of `Memory` / `SSL` / `Logs` / `Raw YAML` page renderers to
  extract the shared header / card / button / form-field / tab-strip patterns.
- Rebuild Plugins page with those conventions exclusively. Drop bespoke
  `.plugins-page` etc.

This is folded into the Task 2 (UX) work below.

---

## Out of scope for v2

- Plugin signing / verification (deferred to v3).
- Auto-update on bundle version change (operator manually re-uploads).
- Plugin marketplace / rating / install counts (outside Phase 2/3).
- Configuration migration on schema_version bump (deferred).
- Plugin sandbox / isolation (Phase 5; matches HA `custom_components` posture
  for v1/v2: trust-the-operator).
