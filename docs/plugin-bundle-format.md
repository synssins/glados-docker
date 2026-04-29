# Plugin bundle format (v2)

**Audience:** operators authoring or repackaging plugin bundles for
GLaDOS. If you only want to install plugins from existing bundles,
the README's *Plugins* section covers the click-path. This document
covers the file format on disk.

**Status:** Live as of Change 33 (2026-04-29). v1 (`server.json`-only)
plugins continue to load through the loader's fallback path; existing
installs are not affected.

---

## What is a plugin bundle

A plugin bundle is a `.zip` file containing a `plugin.json` manifest
and any supporting files the plugin needs at runtime. It is the same
shape as Home Assistant's `custom_components` zips and the VS Code
`.vsix` family: a self-contained archive an operator drops into the
WebUI to install. The manifest is GLaDOS-side only — bundle authors
do not need upstream MCP server cooperation. Take any MCP server from
GitHub, write a `plugin.json`, zip the result, install.

Bundles are uploaded via **Configuration → Plugins → Manage → Upload**
(drag-drop or file picker). Catalogs published as `index.json` URLs
can also point at bundle downloads — see *Catalog publishing* below.

## Bundle layout

```
my-plugin.zip
├── plugin.json          REQUIRED — GLaDOS bundle manifest
├── README.md            OPTIONAL — markdown shown on the plugin's About tab
├── icon.svg             OPTIONAL — Lucide-style icon for the tab strip
├── server.json          OPTIONAL — upstream MCP server.json, used as a
│                        fallback source for settings if plugin.json
│                        omits them; v1 bundles that ship only this
│                        file still load via the loader's v1 fallback
└── src/                 OPTIONAL — bundled source for runtime.mode = "bundled"
    ├── package.json
    ├── pyproject.toml
    └── ...
```

`plugin.json` must sit at the top level of the zip. Nesting it inside
a wrapping folder (`my-plugin/plugin.json`) will fail validation —
zip the folder *contents*, not the folder itself.

## `plugin.json` reference

All bundles declare `schema_version: 1` (the current version of this
schema). Required fields:

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `1` | Only `1` is accepted by this build. |
| `name` | string | Operator-visible plugin title. |
| `description` | string | One-sentence summary; rendered under the title. |
| `version` | string | SemVer; rendered next to the name. |
| `category` | string | Gallery grouping. Known values: `media`, `home`, `integrations`, `system`, `dev`, `utility`. Unknown strings are rendered title-cased. |
| `runtime` | object | One of three runtime-mode shapes — see below. |

Optional fields:

| Field | Type | Notes |
|---|---|---|
| `icon` | string | Lucide icon name (e.g. `tv`, `home`, `terminal`). |
| `persona_role` | `interactive` \| `autonomy` \| `both` | Which lane should see the plugin's tools. Defaults to `both`. |
| `homepage` | string | Link surfaced on the About tab. |
| `settings` | array | Operator-configurable settings — see *Settings types*. |

The internal directory the bundle unpacks into is derived server-side
from `name` (lowercased, non-alphanumeric → `-`, collisions append
`-2`, `-3`). Operators never see this name.

### `runtime.mode` values

A bundle declares exactly one runtime mode. The mode determines how
GLaDOS spawns or connects to the underlying MCP server.

| Mode | Required fields | Behaviour |
|---|---|---|
| `registry` | `package` (e.g. `uvx:demo-mcp@1.0.0` or `npx:@org/demo@1.0.0`) | Spawn via `uvx` / `npx`, fetching from PyPI / npm at runtime. No bundled source needed. Per-plugin cache lives at `<plugin-dir>/.uvx-cache/`. |
| `bundled` | `command`, `args` | Spawn `command + args`. The plugin directory is exposed to the subprocess as `GLADOS_PLUGIN_DIR`. Source must be in the zip. |
| `remote` | `url` (https only), optional `headers` | Connect over streamable-HTTP. No subprocess. |

#### Example — registry mode (`uvx`)

```json
{
  "schema_version": 1,
  "name": "Demo Tools",
  "description": "Example tools fetched from PyPI at runtime.",
  "version": "1.0.0",
  "category": "utility",
  "icon": "terminal",
  "runtime": {
    "mode": "registry",
    "package": "uvx:demo-mcp@1.0.0"
  },
  "settings": [
    {
      "key": "DEMO_API_KEY",
      "label": "Demo API Key",
      "type": "secret",
      "required": true
    }
  ]
}
```

#### Example — bundled mode

```json
{
  "schema_version": 1,
  "name": "Local Notes",
  "description": "Notes server bundled inside the zip.",
  "version": "0.3.0",
  "category": "utility",
  "runtime": {
    "mode": "bundled",
    "command": "node",
    "args": ["src/dist/index.js"]
  },
  "settings": [
    {
      "key": "NOTES_DIR",
      "label": "Notes directory",
      "type": "text",
      "default": "/data/notes"
    }
  ]
}
```

#### Example — remote mode

```json
{
  "schema_version": 1,
  "name": "Home Assistant",
  "description": "HA tools via the built-in MCP server.",
  "version": "1.0.0",
  "category": "home",
  "runtime": {
    "mode": "remote",
    "url": "https://ha.example.test/api/mcp"
  },
  "settings": [
    {
      "key": "Authorization",
      "label": "Long-lived access token",
      "type": "secret",
      "required": true,
      "description": "Sent as the Authorization header."
    }
  ]
}
```

### Settings types

Each entry in `settings[]` describes one operator-configurable input.
The `key` is the name passed to the spawned subprocess (as an env var)
or to the remote endpoint (as a header for `remote` mode); the `label`
is what operators see in the configuration form.

| Type | Form widget | Stored as | Notes |
|---|---|---|---|
| `text` | text input | plain runtime value | General string. |
| `url` | URL input | plain runtime value | Browser-side URL validation. |
| `number` | number input | plain runtime value | Numeric. |
| `boolean` | checkbox | plain runtime value | Toggle. |
| `select` | dropdown | plain runtime value | Requires `choices: [...]`. |
| `secret` | password input | encrypted secrets store (mode 0600) | Masked as `***` when re-read by the form; the `***` sentinel preserves the existing value on partial save. |

Per-setting fields:

| Field | Required | Notes |
|---|---|---|
| `key` | yes | Subprocess env-var name / remote header name. Operator never sees this. |
| `label` | yes | Form label. Operator-friendly phrasing — e.g. "Sonarr API Key", not `SONARR_API_KEY`. |
| `type` | yes | One of the six above. |
| `required` | no | Defaults to `false`. Required settings without a value block the plugin from spawning. |
| `description` | no | Helper text rendered below the input. |
| `default` | no | Pre-fills the form. Not applied automatically — the operator confirms by saving. |
| `choices` | conditional | Required when `type == "select"`. Array of strings. |

## Tutorial — wrap any MCP server in 5 minutes

This walks through repackaging an upstream MCP server as a GLaDOS
bundle without modifying upstream code.

### 1. Pick a server

The Model Context Protocol project lists examples at
`https://modelcontextprotocol.io/examples`, and the official npm /
PyPI catalogues host many third-party servers. For this tutorial,
assume an upstream package `@example/notes-mcp` published to npm at
version `0.5.0`, which expects a single env var `NOTES_DIR`.

### 2. Decide on the runtime mode

If the server is already published to a registry (PyPI or npm) and you
just want GLaDOS to run it, use `registry` mode — no bundled source
needed. If you want to ship a specific commit or a private fork,
clone the repo, copy it into a `src/` folder inside your bundle, and
use `bundled` mode.

For the `@example/notes-mcp` case, registry mode is enough.

### 3. Author `plugin.json`

Create a directory on disk:

```
notes-bundle/
└── plugin.json
```

Contents of `plugin.json`:

```json
{
  "schema_version": 1,
  "name": "Notes",
  "description": "Read and write notes from your local notes directory.",
  "version": "0.5.0",
  "category": "utility",
  "icon": "notebook",
  "homepage": "https://example.test/notes-mcp",
  "runtime": {
    "mode": "registry",
    "package": "npx:@example/notes-mcp@0.5.0"
  },
  "settings": [
    {
      "key": "NOTES_DIR",
      "label": "Notes directory",
      "type": "text",
      "required": true,
      "description": "Absolute path inside the GLaDOS container, e.g. /data/notes",
      "default": "/data/notes"
    }
  ]
}
```

Two things to flag:

- **Operator-friendly `label`.** Operators see "Notes directory", not
  the env-var name `NOTES_DIR`. The env var is still what gets handed
  to the subprocess at spawn time — it just isn't shown in the form.
- **Category.** `utility` is a known category and renders as
  *Utility*. Any string works; unknown strings are rendered
  title-cased.

### 4. Zip it

From inside `notes-bundle/`, zip the *contents* (not the wrapping
folder):

```
zip -r notes.zip plugin.json
```

If you used `bundled` mode and added a `src/` folder, include it too:
`zip -r notes.zip plugin.json src/`. Result: a `notes.zip` whose top
level is `plugin.json`.

### 5. Install via the WebUI

1. Sign in to GLaDOS.
2. Go to **Configuration → Plugins → Manage**.
3. Drop `notes.zip` onto the **Upload** card (or click *choose file*
   and pick it). The form switches to the new plugin's tab on success.
4. Fill in **Notes directory**, save, and toggle **Enabled**.
5. The plugin's tools become available to the LLM immediately — no
   container restart required.

## Catalog publishing

The **Browse** tab lists bundles from any number of `index.json`
catalog URLs configured under Configuration → Plugins → Browse →
*Index URLs*. A catalog entry pointing at a bundle looks like:

```json
{
  "schema_version": 1,
  "plugins": [
    {
      "name": "notes",
      "title": "Notes",
      "category": "utility",
      "description": "Read and write notes.",
      "bundle_url": "https://example.test/bundles/notes-0.5.0.zip"
    }
  ]
}
```

The Browse tab fetches `bundle_url` and pipes the bytes through the
same upload pipeline an operator would trigger by hand. Hosting
requirements: https only, served as `application/zip` (or any
binary type the browser can fetch).

The curated `synssins/glados-plugins` repo is the reference catalog
for first-party bundles. Its content rolls out under Phase 3.

## Safety constraints

The upload pipeline rejects bundles that violate any of the following:

- **Compressed size** > 50 MB.
- **Total uncompressed size** > 200 MB.
- **Per-entry uncompressed size** > 50 MB.
- **Symlinks** anywhere in the archive.
- **Path traversal** — any entry whose resolved path lands outside
  the staging directory.
- **Absolute paths** — entries beginning with `/` or `\`.
- **Missing or unparseable `plugin.json`** at the top level.
- **`plugin.json` that fails schema validation** — the operator gets
  the Pydantic error verbatim.

These guards run before any bytes are extracted to disk; an unsafe
bundle never lands in `/app/data/plugins/`. A failed install rolls
back the staging directory and surfaces a 4xx error in the WebUI.

## Backward compatibility

v1 plugins (those installed before Change 33 via the old Add-by-URL
flow, with `server.json` and no `plugin.json`) continue to load
without modification. The loader checks for `plugin.json` first; if
missing, it parses `server.json` and synthesises a v2 view via the
internal `v1_to_v2` helper, so the form renderer and runner see a
single shape regardless of bundle vintage.

There is no live-migration step. Operators do not need to re-author
existing plugins.

## See also

- [`docs/plugins-architecture.md`](plugins-architecture.md) — internal
  module layout, loader / runner / store, trust posture, phasing.
- [`docs/CHANGES.md`](CHANGES.md) Change 33 — the v1 → v2 migration
  log entry.
- README *Plugins* section — operator click-path for installing from
  Browse or Upload.
