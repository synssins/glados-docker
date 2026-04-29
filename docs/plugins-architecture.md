# GLaDOS Plugin Architecture

**Status:** Phase 2 — scaffolding landing 2026-04-29 (this document); WebUI surface +
runtime spawn follow up next session. The manifest layer, loader, and `MCPServerConfig`
conversion ship as part of Change 31.

**Audience:** anyone touching plugin code or planning new plugins.
Operators read the README's "Plugins" section instead.

---

## Goals

1. **Anything-talks-to-anything.** Operator should be able to add a capability to GLaDOS
   without writing code in this repo. If a service has an MCP server (1000+ on
   GitHub as of late 2025), it should be installable as a plugin.
2. **Self-contained container.** Plugins run inside the GLaDOS container — no
   sidecar Docker setup required for the common case. Stronger isolation
   (Phase 5) is a documented escape hatch, not the primary path.
3. **Survives container rebuilds.** Plugins live on the `/app/data` volume,
   not in the image. `docker compose pull && up -d` doesn't wipe them.
4. **No GLaDOS-side hardcoding per plugin.** Adding `mcp-arr` doesn't require
   editing GLaDOS source. The plugin self-describes via the standard
   `server.json` manifest; GLaDOS reads it generically.
5. **Standard compliance.** Plugins are MCP servers conforming to the official
   `server.json` spec (current schema `2025-12-11`). No invented format.

## Why MCP and `server.json`

Researched in conversation 2026-04-29:

- MCP was donated to the Linux Foundation's Agentic AI Foundation in
  December 2025 (Anthropic, Block, OpenAI co-founders). It is the
  industry-standard tool-integration protocol; vendor-neutral.
- The MCP project maintains an official server-metadata schema at
  `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`.
  This is the format the [Official MCP Registry](https://registry.modelcontextprotocol.io/)
  uses for publishing.
- `server.json` declares everything a UI needs to render a config form:
  `environmentVariables[]` with `description`, `default`, `isRequired`,
  `isSecret`, `format`, `choices`. Plus `packageArguments[]` /
  `runtimeArguments[]` for CLI args, `remotes[]` for HTTP-transport
  servers, `_meta` for vendor extensions.
- HA already ships `mcp_server` (HA 2025.2+) — exposes HA services as MCP
  tools at `/api/mcp`. Wiring HA into GLaDOS = one plugin entry, no
  custom HA-specific code.

We picked `server.json` over alternatives:

- **Claude Desktop's `claude_desktop_config.json`** — operator-side
  runtime config (`command + args + env`); no schema. Useful as a fallback
  format the "Custom plugin" form accepts, but inadequate as a publisher
  manifest.
- **VS Code's `inputs[]` array** — VS Code-specific extension over
  `mcp.json`; subsumed by `server.json`'s richer `environmentVariables[]`.
- **Custom GLaDOS format** — re-inventing the wheel. Locks plugins out
  of the broader ecosystem.

## Storage layout

Per-plugin directory under `/app/data/plugins/`:

```
/app/data/plugins/
├── mcp-arr/
│   ├── server.json          ← upstream manifest, read-only-ish, drives form rendering
│   ├── runtime.yaml         ← operator's resolved values + which package[] entry was selected
│   ├── secrets.env          ← isSecret:true env values, mode 0600, never logged/exported plain
│   └── .uvx-cache/          ← runtime spawn cache (Phase 2 follow-up)
├── mcp-spotify/
│   ├── server.json
│   ├── runtime.yaml
│   └── secrets.env
└── ha-assist/               ← remote streamable-HTTP plugin, no install needed
    ├── server.json          ← uses `remotes[]` not `packages[]`
    └── runtime.yaml
```

### `runtime.yaml` shape

```yaml
plugin: mcp-arr
server_json_version: "2025-12-11"
enabled: true
package_index: 0          # which packages[] entry (or null if remotes[])
remote_index: null        # which remotes[] entry (or null if packages[])
env_values:
  SONARR_URL: "http://sonarr.lan:8989"
  RADARR_URL: "http://radarr.lan:7878"
  # secrets are in secrets.env, not here
header_values: {}
arg_values: {}
```

Non-secret env / header / arg values live in `runtime.yaml`.
`isSecret: true` values live in `secrets.env`. Both are merged at spawn
time into the subprocess environment / HTTP headers.

### `secrets.env` shape

KEY=VALUE per line, mode 0600. Read by the loader at spawn time and
merged with `runtime.yaml` env_values (secrets.env wins). Never logged,
never exported in plain via the WebUI (mask on read).

## `_meta` extensions GLaDOS understands

All under `com.synssins.glados/*` (reverse-DNS namespace, per
`server.json` spec):

| Key | Value | Default | Meaning |
|---|---|---|---|
| `com.synssins.glados/category` | string | `"utility"` | Gallery grouping. Suggested set: `media`, `home`, `system`, `dev`, `utility`. |
| `com.synssins.glados/icon` | string | `"plug"` | Lucide icon name for the gallery tile. |
| `com.synssins.glados/min_glados_version` | string (SemVer) | unset | Refuse to install if container version is below this. |
| `com.synssins.glados/recommended_persona_role` | `"interactive"` \| `"autonomy"` \| `"both"` | `"both"` | Hint for which lane should see the plugin's tools by default. |

Custom keys outside this namespace MUST NOT be invented — use the
existing `_meta` fields or propose a spec update.

## Module layout

```
glados/plugins/
├── __init__.py          ← public API (discover, load_one, errors)
├── manifest.py          ← Pydantic models for server.json + runtime.yaml
├── loader.py            ← walks /app/data/plugins, parses, validates
├── runner.py            ← manifest → MCPServerConfig (the existing MCP infra)
├── store.py             ← reads/writes runtime.yaml + secrets.env
└── errors.py            ← PluginError, ManifestError, InstallError
```

### Manifest parser (`manifest.py`)

`ServerJSON` is a Pydantic model that validates against the official
`server.json` schema. Extra fields are allowed under `_meta` per spec
but rejected at top level (strict). The `2025-12-11` schema is the
floor; older schemas are rejected with a clear error message pointing
the operator at the upstream plugin's repo to update.

### Loader (`loader.py`)

`discover_plugins(plugins_dir)` walks `<plugins_dir>/*/`, expects each
subdir to contain `server.json` + `runtime.yaml` + (optionally)
`secrets.env`. Returns `list[Plugin]` where `Plugin` bundles the parsed
manifest, runtime config, and resolved secret env. Skips plugins where
`runtime.yaml.enabled: false` or where validation fails (logs warning,
continues).

### Runner (`runner.py`)

`to_mcp_server_config(plugin)` translates a parsed `Plugin` into a
`glados.mcp.config.MCPServerConfig`. Mapping:

| `server.json` | `MCPServerConfig` |
|---|---|
| `packages[i].transport.type == "stdio"` | `transport="stdio"`, `command` and `args` derived from `runtimeHint` + `packageArguments[]` |
| `remotes[i].type == "streamable-http"` | `transport="http"`, `url`, `headers` from `remotes[i].headers[]` resolved values |
| `remotes[i].type == "sse"` | `transport="sse"`, ditto |
| `runtime.yaml.env_values` + `secrets.env` | `env` |
| `_meta["com.synssins.glados/recommended_persona_role"]` | informational only — Phase 2 use it for tool routing hints |

For Phase 2 (this commit) the runner ONLY emits `MCPServerConfig` and
hands it to the existing `MCPManager`. **Actual subprocess spawning
for stdio plugins (the `uvx <package>` invocation) is a Phase 2
follow-up** — until then, only `remotes[]` plugins (HA, etc.) work
end-to-end. Stdio plugins parse cleanly but don't spawn yet, so the
loader marks them with a "needs runtime" warning.

### Hot-reload (Phase 2 follow-up)

Not in this commit. Next session adds:
- WebUI panel under System → Services → Plugins
- Add / Remove / Update / Enable / Disable controls
- `MCPManager.reload(new_configs)` — rotate sessions without restarting
  the container
- File-watcher on `/app/data/plugins/` for drop-in additions

## Curated catalog

Lives at `synssins/glados-plugins` (GitHub repo, to be created in
Phase 3). Top-level `index.json`:

```json
{
  "schema_version": 1,
  "plugins": [
    {
      "name": "io.github.aplaceforallmystuff/mcp-arr",
      "title": "*arr Stack",
      "category": "media",
      "server_json_url": "https://raw.githubusercontent.com/synssins/glados-plugins/main/plugins/mcp-arr/server.json",
      "min_glados_version": "1.0.0"
    }
  ]
}
```

WebUI fetches this on the "Browse Plugins" tab. Each entry's
`server_json_url` is the canonical schema GLaDOS parses to render the
install form. The curated repo owns the `server.json` files for plugins
whose upstream doesn't ship one yet (most of the ecosystem in
2026-04 still pre-server.json).

## Trust posture

Plugins run as the container's user. **No sandbox in v1.** This matches
Home Assistant's `custom_components` posture. The curated catalog pins
versions and links upstream audits where they exist. The "Custom
plugin" form in the WebUI bold-warns operators that they're running
arbitrary code from arbitrary sources.

Phase 5 (deferred) adds an opt-in sidecar mode: plugin runs in its own
Docker container, GLaDOS connects via streamable-HTTP across the bridge
network. Strong isolation, more compose surface to manage.

## Phasing summary

| Phase | Scope | Status |
|---|---|---|
| 1 | Wire HA's `/api/mcp` server into existing `MCPManager` | Not started |
| **2 (scaffolding)** | **`glados/plugins/` package: manifest, loader, runner, store. Engine consumes plugins at startup. No WebUI yet, no stdio spawn yet.** | **2026-04-29 (Change 31)** |
| 2 (full) | WebUI Plugins panel, `uvx` / `npx` runtime spawn, hot-reload | Next |
| 3 | Curated `synssins/glados-plugins` repo + "Browse Plugins" tab | Next-after-next |
| 4 | GLaDOS as MCP server (port 8017, streamable-HTTP, TLS-wrapped via existing helper) | Deferred |
| 5 | Sidecar/external escape hatch | Deferred |

## References

- [MCP Registry — server.json spec (2025-12-11)](https://raw.githubusercontent.com/modelcontextprotocol/registry/refs/heads/main/docs/reference/server-json/generic-server-json.md)
- [Official MCP Registry](https://registry.modelcontextprotocol.io/)
- [HA `mcp_server` integration](https://www.home-assistant.io/integrations/mcp_server/)
- [HA `mcp` (client) integration](https://www.home-assistant.io/integrations/mcp/)
- [@thelord/mcp-arr (npm)](https://www.npmjs.com/package/@thelord/mcp-arr)
- [aplaceforallmystuff/mcp-arr (GitHub)](https://github.com/aplaceforallmystuff/mcp-arr)
- [Anthropic — Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
