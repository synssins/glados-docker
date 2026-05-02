# Plugin Bundle Format Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the v1 `server.json`-fetch-by-URL flow with a self-contained zip bundle format. Bundle has `plugin.json` (operator-facing), optional `server.json` fallback, optional bundled source. Two install paths: Browse (catalog) and Upload (drag-drop). Operator-friendly terminology throughout — no `slug` / `manifest` / env-var keys visible.

**Architecture:** Three-task split. Task 1 = backend bundle format + zip-install endpoint. Task 2 = WebUI rework (drop Add-by-URL, add Upload, design-system conformance, terminology sweep). Task 3 = docs (architecture migration note, new bundle-format authoring guide, CHANGES Change 33). Each task ships as one commit; final deploy after Task 3.

**Tech Stack:** Python 3.12 + Pydantic v2 + `zipfile` (stdlib, no new deps); vanilla JS + drag-drop + multipart upload; existing scripts/_local_deploy.py for deploy. Spec at `docs/superpowers/specs/2026-04-29-plugin-bundle-format-design.md`.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `glados/plugins/bundle.py` | `PluginJSON` Pydantic model + `Setting` + `RuntimeMode` + v1→v2 synthetic conversion helper | **Create** |
| `glados/plugins/loader.py` | `load_plugin` prefers `plugin.json`, falls back to `server.json` | Modify |
| `glados/plugins/runner.py` | Runtime mapping for `registry` / `bundled` / `remote` modes | Modify |
| `glados/plugins/store.py` | `install_from_zip(zip_bytes, plugins_dir) -> Plugin` with safety guards | Modify |
| `glados/webui/plugin_endpoints.py` | New `install_from_zip_bytes` helper. Remove `install_from_url` exports. | Modify |
| `glados/webui/tts_ui.py` | New `POST /api/plugins/upload` (multipart). Remove the `install` endpoint route. | Modify |
| `glados/webui/static/ui.js` | Drop Add-by-URL inline section. Add Upload section (drag-drop + file picker + progress). Form rendering uses `setting.label`. Category title-case label map. Page-conformance pass. | Modify |
| `glados/webui/static/style.css` | Upload drop-zone styling. Drop bespoke `.plugins-page` / `.plugin-header-card` classes — adopt existing page conventions. | Modify |
| `tests/test_plugins_bundle.py` | Schema validation, v1→v2 conversion, runtime mode roundtrip | **Create** |
| `tests/test_plugins_zip_install.py` | Safe extract: traversal, symlink, oversize, archive bomb. Atomic rename. | **Create** |
| `tests/test_webui_plugins.py` | Extend: upload endpoint round-trip + multipart parsing | Modify |
| `docs/plugin-bundle-format.md` | Schema reference + "wrap any MCP server in 5 minutes" guide | **Create** |
| `docs/plugins-architecture.md` | v1→v2 migration note | Modify |
| `docs/CHANGES.md` | Change 33 entry | Modify |
| `README.md` | Update Plugins section to reflect new install flow | Modify |

---

## Task 1: Backend bundle format + zip-install endpoint

**Goal:** `glados/plugins/bundle.py` defines the v2 schema; loader prefers `plugin.json` and falls back to `server.json`; runner handles `registry`/`bundled`/`remote` modes; `install_from_zip` extracts safely and atomically renames into place; `POST /api/plugins/upload` accepts multipart and invokes the install pipeline.

**Files:**
- Create: `glados/plugins/bundle.py`
- Modify: `glados/plugins/loader.py`
- Modify: `glados/plugins/runner.py`
- Modify: `glados/plugins/store.py`
- Modify: `glados/plugins/__init__.py` (re-exports)
- Modify: `glados/webui/plugin_endpoints.py`
- Modify: `glados/webui/tts_ui.py`
- Create: `tests/test_plugins_bundle.py`
- Create: `tests/test_plugins_zip_install.py`
- Modify: `tests/test_webui_plugins.py`

**Acceptance Criteria:**
- [ ] `PluginJSON` Pydantic model with required fields (`schema_version`, `name`, `description`, `version`, `category`, `runtime`) and optional (`description`, `version`, `icon`, `persona_role`, `homepage`, `settings`).
- [ ] Three runtime modes parse: `registry` (requires `package`), `bundled` (requires `command` + `args`), `remote` (requires `url`).
- [ ] Six setting types: `text`, `url`, `number`, `boolean`, `select` (requires `choices`), `secret`.
- [ ] `load_plugin` checks `plugin.json` first, falls back to `server.json` v1 path. New synthetic conversion `_v1_to_v2(server_json) -> PluginJSON` for the form renderer to consume a single shape.
- [ ] `install_from_zip(zip_bytes: bytes, plugins_dir: Path) -> Path`:
  - Rejects content > 50 MB compressed.
  - Rejects total uncompressed > 200 MB.
  - Rejects per-entry uncompressed > 50 MB.
  - Rejects symlinks (POSIX bit `0o120000`).
  - Rejects path traversal (any extracted path must be inside the staging dir).
  - Atomic via `<name>.installing/` → `<name>/` rename.
  - Returns the final path.
- [ ] `POST /api/plugins/upload` accepts multipart upload with file field name `bundle`. Reads bytes, calls `install_from_zip`, returns `{name, internal_name, plugin: <PluginJSON dump>}`. 4xx with clear message on validation failure.
- [ ] `POST /api/plugins/install` (v1 URL fetch) is REMOVED from the dispatcher. The helper `install_from_url` may stay in the module (unexported from `__init__`) but its endpoint is gone.
- [ ] 25+ new tests pass.

**Verify:** `python -m pytest tests/test_plugins_bundle.py tests/test_plugins_zip_install.py tests/test_webui_plugins.py -v`

**Steps:**

- [ ] **Step 1: Write `tests/test_plugins_bundle.py` (failing first)**

```python
"""PluginJSON schema, runtime modes, settings, v1→v2 conversion."""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def _minimal(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "name": "Demo Plugin",
        "description": "A demo plugin.",
        "version": "1.0.0",
        "category": "utility",
        "runtime": {"mode": "registry", "package": "uvx:demo-mcp@1.0.0"},
    }
    base.update(overrides)
    return base


def test_minimal_plugin_json_parses():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal())
    assert p.name == "Demo Plugin"
    assert p.runtime.mode == "registry"
    assert p.runtime.package == "uvx:demo-mcp@1.0.0"


def test_runtime_registry_requires_package():
    from glados.plugins.bundle import PluginJSON
    with pytest.raises(ValidationError, match="package"):
        PluginJSON.model_validate(_minimal(runtime={"mode": "registry"}))


def test_runtime_bundled_requires_command_and_args():
    from glados.plugins.bundle import PluginJSON
    with pytest.raises(ValidationError, match="command|args"):
        PluginJSON.model_validate(_minimal(runtime={"mode": "bundled"}))


def test_runtime_bundled_with_command():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal(
        runtime={"mode": "bundled", "command": "node", "args": ["src/index.js"]}
    ))
    assert p.runtime.command == "node"
    assert p.runtime.args == ["src/index.js"]


def test_runtime_remote_requires_https_url():
    from glados.plugins.bundle import PluginJSON
    with pytest.raises(ValidationError, match="https"):
        PluginJSON.model_validate(_minimal(
            runtime={"mode": "remote", "url": "http://x.test/mcp"}
        ))


def test_runtime_remote_with_https_url():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal(
        runtime={"mode": "remote", "url": "https://x.test/mcp"}
    ))
    assert str(p.runtime.url) == "https://x.test/mcp"


def test_settings_text_default():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal(settings=[
        {"key": "FOO", "label": "Foo", "type": "text"}
    ]))
    assert p.settings[0].label == "Foo"
    assert p.settings[0].is_required is False


def test_settings_select_requires_choices():
    from glados.plugins.bundle import PluginJSON
    with pytest.raises(ValidationError, match="choices"):
        PluginJSON.model_validate(_minimal(settings=[
            {"key": "Q", "label": "Q", "type": "select"}
        ]))


def test_settings_secret_type_accepted():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal(settings=[
        {"key": "API_KEY", "label": "API Key", "type": "secret", "required": True}
    ]))
    assert p.settings[0].type == "secret"
    assert p.settings[0].is_required is True


def test_category_unknown_string_accepted_as_literal():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal(category="custom-bucket"))
    assert p.category == "custom-bucket"


def test_v1_server_json_to_v2_conversion_remote():
    """v1 server.json with remotes[] → synthetic plugin.json with mode=remote."""
    from glados.plugins.bundle import v1_to_v2
    server_json = {
        "name": "io.example/demo",
        "description": "demo",
        "version": "1.0.0",
        "remotes": [{
            "type": "streamable-http",
            "url": "https://x.test/mcp",
            "headers": [{"name": "Authorization", "isRequired": True, "isSecret": True}],
        }],
    }
    p = v1_to_v2(server_json, package_index=None, remote_index=0)
    assert p.runtime.mode == "remote"
    assert str(p.runtime.url) == "https://x.test/mcp"
    assert p.settings[0].key == "Authorization"
    assert p.settings[0].type == "secret"


def test_v1_server_json_to_v2_conversion_registry():
    """v1 server.json with packages[uvx] → synthetic plugin.json with mode=registry."""
    from glados.plugins.bundle import v1_to_v2
    server_json = {
        "name": "demo.python",
        "description": "demo",
        "version": "1.0.0",
        "packages": [{
            "registryType": "pypi",
            "identifier": "demo-mcp",
            "version": "1.0.0",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
            "environmentVariables": [
                {"name": "DEMO_KEY", "isRequired": True, "isSecret": True}
            ],
        }],
    }
    p = v1_to_v2(server_json, package_index=0, remote_index=None)
    assert p.runtime.mode == "registry"
    assert p.runtime.package == "uvx:demo-mcp@1.0.0"
    assert p.settings[0].type == "secret"
```

Run: `python -m pytest tests/test_plugins_bundle.py -v`
Expected: FAIL — `glados.plugins.bundle` module doesn't exist.

- [ ] **Step 2: Create `glados/plugins/bundle.py`**

```python
"""GLaDOS plugin bundle manifest (`plugin.json`) — v2 schema.

Replaces the v1 `server.json`-only flow. A bundle is a zip containing
`plugin.json` at the top level plus optional `server.json` (fallback),
README, icon, and bundled source.

See ``docs/superpowers/specs/2026-04-29-plugin-bundle-format-design.md``
for the canonical design.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class Setting(BaseModel):
    """One operator-configurable setting on the plugin."""
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: Literal["text", "url", "number", "boolean", "select", "secret"]
    required: bool = False
    description: str | None = None
    default: str | int | float | bool | None = None
    choices: list[str] | None = None

    @model_validator(mode="after")
    def _select_requires_choices(self) -> "Setting":
        if self.type == "select" and not self.choices:
            raise ValueError("select-type setting requires non-empty 'choices'")
        return self

    @property
    def is_required(self) -> bool:
        return self.required


class RegistryRuntime(BaseModel):
    """Spawn via uvx/npx fetching at runtime from PyPI/npm."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["registry"] = "registry"
    package: str  # form: "uvx:pkg@ver" or "npx:pkg@ver" or "dnx:pkg@ver"

    @field_validator("package")
    @classmethod
    def _well_formed_package(cls, v: str) -> str:
        if ":" not in v or "@" not in v.split(":", 1)[1]:
            raise ValueError(
                f"package must be of form '<runtime>:<name>@<version>', got {v!r}"
            )
        rt = v.split(":", 1)[0]
        if rt not in ("uvx", "npx", "dnx"):
            raise ValueError(f"unsupported runtime {rt!r}; want uvx/npx/dnx")
        return v


class BundledRuntime(BaseModel):
    """Spawn `command + args` from inside the unpacked bundle directory."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["bundled"] = "bundled"
    command: str
    args: list[str] = Field(default_factory=list)


class RemoteRuntime(BaseModel):
    """Connect via streamable-HTTP to an external endpoint."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["remote"] = "remote"
    url: HttpUrl
    headers: list[Setting] | None = None  # operator-resolved at install

    @field_validator("url")
    @classmethod
    def _https_only(cls, v: HttpUrl) -> HttpUrl:
        if v.scheme != "https":
            raise ValueError(f"remote URL must be https://, got {v}")
        return v


Runtime = RegistryRuntime | BundledRuntime | RemoteRuntime


class PluginJSON(BaseModel):
    """Top-level `plugin.json` document."""
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1)
    name: str
    description: str
    version: str
    category: str  # 'media' / 'home' / 'system' / 'dev' / 'utility' or anything
    icon: str | None = None
    persona_role: Literal["interactive", "autonomy", "both"] = "both"
    homepage: str | None = None
    runtime: Runtime = Field(discriminator="mode")
    settings: list[Setting] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _v1_only(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"unsupported schema_version {v}; this build only handles v1")
        return v


def v1_to_v2(server_json: dict, *, package_index: int | None, remote_index: int | None) -> PluginJSON:
    """Synthesize a PluginJSON view of a legacy v1 server.json so the WebUI
    form renderer and runner can treat all installs as v2.

    Exactly one of package_index / remote_index must be set (matches the
    Phase 2a runtime.yaml selector convention)."""
    name = server_json.get("name", "unknown")
    description = server_json.get("description", "")
    version = server_json.get("version", "0.0.0")
    meta = server_json.get("_meta") or {}
    category = meta.get("com.synssins.glados/category", "utility")
    icon = meta.get("com.synssins.glados/icon")
    persona_role = meta.get("com.synssins.glados/recommended_persona_role", "both")
    if persona_role not in ("interactive", "autonomy", "both"):
        persona_role = "both"

    if package_index is not None:
        pkg = server_json["packages"][package_index]
        rt_hint = pkg.get("runtimeHint")
        if not rt_hint:
            raise ValueError(f"v1 package missing runtimeHint")
        package = f"{rt_hint}:{pkg['identifier']}@{pkg['version']}"
        runtime: Runtime = RegistryRuntime(package=package)
        settings = [
            Setting(
                key=ev["name"],
                label=ev["name"],  # v1 has no operator-friendly label; use key
                type="secret" if ev.get("isSecret") else "text",
                required=ev.get("isRequired", False),
                description=ev.get("description"),
                default=ev.get("default"),
                choices=ev.get("choices"),
            )
            for ev in pkg.get("environmentVariables", [])
        ]
    elif remote_index is not None:
        rem = server_json["remotes"][remote_index]
        runtime = RemoteRuntime(url=rem["url"])
        settings = [
            Setting(
                key=h["name"],
                label=h["name"],
                type="secret" if h.get("isSecret") else "text",
                required=h.get("isRequired", False),
                description=h.get("description"),
                default=h.get("default"),
            )
            for h in rem.get("headers", [])
        ]
    else:
        raise ValueError("v1_to_v2 requires package_index or remote_index")

    return PluginJSON(
        schema_version=1,
        name=name,
        description=description or "(no description)",
        version=version,
        category=category,
        icon=icon,
        persona_role=persona_role,
        runtime=runtime,
        settings=settings,
    )
```

Run tests: `python -m pytest tests/test_plugins_bundle.py -v` → 12 passed.

- [ ] **Step 3: Write `tests/test_plugins_zip_install.py` (failing)**

```python
"""install_from_zip safety + atomicity."""
from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest


def _make_zip(files: dict[str, bytes | str], symlinks: list[tuple[str, str]] | None = None) -> bytes:
    """Build an in-memory zip. files = {name: content}. symlinks = [(name, target)]."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            zf.writestr(name, data)
        for name, target in symlinks or []:
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 & 0xFFFF) << 16  # symlink type
            zf.writestr(info, target)
    return buf.getvalue()


def _good_plugin_json() -> str:
    return json.dumps({
        "schema_version": 1,
        "name": "Demo Plugin",
        "description": "x",
        "version": "1.0.0",
        "category": "utility",
        "runtime": {"mode": "registry", "package": "uvx:demo@1.0.0"},
    })


def test_install_from_zip_happy_path(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    zip_bytes = _make_zip({"plugin.json": _good_plugin_json()})
    final = install_from_zip(zip_bytes, tmp_path)
    assert (final / "plugin.json").exists()
    assert (final / "plugin.json").read_text().strip().startswith("{")


def test_install_from_zip_rejects_path_traversal(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    zip_bytes = _make_zip({
        "../escape.txt": b"x",
        "plugin.json": _good_plugin_json(),
    })
    with pytest.raises(InstallError, match="escape|traversal"):
        install_from_zip(zip_bytes, tmp_path)


def test_install_from_zip_rejects_absolute_path(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    zip_bytes = _make_zip({
        "/etc/passwd": b"x",
        "plugin.json": _good_plugin_json(),
    })
    with pytest.raises(InstallError):
        install_from_zip(zip_bytes, tmp_path)


def test_install_from_zip_rejects_symlink(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    zip_bytes = _make_zip(
        {"plugin.json": _good_plugin_json()},
        symlinks=[("link.txt", "/etc/passwd")],
    )
    with pytest.raises(InstallError, match="symlink"):
        install_from_zip(zip_bytes, tmp_path)


def test_install_from_zip_rejects_oversize_compressed(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    big = b"x" * (51 * 1024 * 1024)  # 51 MB compressed
    with pytest.raises(InstallError, match="too large|size"):
        install_from_zip(big, tmp_path)


def test_install_from_zip_rejects_oversize_uncompressed(tmp_path: Path, monkeypatch):
    """A zip-bomb scenario: small compressed, huge uncompressed."""
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    # Create a zip with one entry whose declared file_size is 201 MB
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo("big.bin")
        info.file_size = 201 * 1024 * 1024  # declared
        zf.writestr(info, b"x")  # actual tiny
        zf.writestr("plugin.json", _good_plugin_json())
    with pytest.raises(InstallError, match="too large|uncompressed"):
        install_from_zip(buf.getvalue(), tmp_path)


def test_install_from_zip_rejects_missing_plugin_json(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    zip_bytes = _make_zip({"README.md": "no plugin.json here"})
    with pytest.raises(InstallError, match="plugin.json"):
        install_from_zip(zip_bytes, tmp_path)


def test_install_from_zip_rejects_invalid_plugin_json(tmp_path: Path):
    from glados.plugins.store import install_from_zip
    from glados.plugins.errors import InstallError
    zip_bytes = _make_zip({"plugin.json": "{not json"})
    with pytest.raises(InstallError, match="plugin.json|JSON"):
        install_from_zip(zip_bytes, tmp_path)


def test_install_from_zip_atomic_via_staging(tmp_path: Path):
    """If validation passes and rename succeeds, no .installing dir leaks."""
    from glados.plugins.store import install_from_zip
    zip_bytes = _make_zip({"plugin.json": _good_plugin_json()})
    install_from_zip(zip_bytes, tmp_path)
    leftover = list(tmp_path.glob("*.installing"))
    assert leftover == []


def test_install_from_zip_collision_appends_suffix(tmp_path: Path):
    """If demo-plugin/ exists, second install lands at demo-plugin-2/."""
    from glados.plugins.store import install_from_zip
    zip_bytes = _make_zip({"plugin.json": _good_plugin_json()})
    first = install_from_zip(zip_bytes, tmp_path)
    second = install_from_zip(zip_bytes, tmp_path)
    assert first != second
    assert second.name.endswith("-2")
```

Run: `python -m pytest tests/test_plugins_zip_install.py -v`
Expected: FAIL — `install_from_zip` doesn't exist yet.

- [ ] **Step 4: Implement `install_from_zip` in `glados/plugins/store.py`**

Append to `glados/plugins/store.py` (alongside the existing helpers):

```python
import io
import json as _json
import os
import zipfile

from .bundle import PluginJSON

MAX_ZIP_BYTES = 50 * 1024 * 1024            # 50 MB compressed
MAX_TOTAL_UNCOMPRESSED = 200 * 1024 * 1024  # 200 MB total uncompressed
MAX_ENTRY_UNCOMPRESSED = 50 * 1024 * 1024   # 50 MB per entry


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract zf into dest with traversal/symlink/size guards."""
    total = 0
    dest_abs = dest.resolve()
    for member in zf.infolist():
        # Reject symlinks (POSIX file-type 0o120000).
        ftype = (member.external_attr >> 16) & 0o170000
        if ftype == 0o120000:
            raise InstallError(f"zip contains a symlink ({member.filename!r}); refusing")
        # Reject absolute paths and traversal.
        if member.filename.startswith("/") or member.filename.startswith("\\"):
            raise InstallError(f"zip member {member.filename!r} is an absolute path")
        try:
            target = (dest / member.filename).resolve()
        except (OSError, ValueError):
            raise InstallError(f"zip member {member.filename!r} resolves outside dest")
        if dest_abs not in target.parents and target != dest_abs:
            raise InstallError(f"zip member {member.filename!r} escapes target dir")
        # Reject oversize entries (per-entry + running total).
        if member.file_size > MAX_ENTRY_UNCOMPRESSED:
            raise InstallError(
                f"zip member {member.filename!r} too large "
                f"({member.file_size} bytes; max {MAX_ENTRY_UNCOMPRESSED})"
            )
        total += member.file_size
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise InstallError(
                f"zip total uncompressed size exceeds {MAX_TOTAL_UNCOMPRESSED} bytes"
            )
    zf.extractall(dest)


def install_from_zip(zip_bytes: bytes, plugins_dir: Path) -> Path:
    """Install a v2 plugin bundle from raw zip bytes. Returns the final
    plugin directory (e.g. plugins_dir/demo-plugin/). Raises InstallError
    on any validation failure."""
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise InstallError(
            f"bundle too large ({len(zip_bytes)} bytes; max {MAX_ZIP_BYTES})"
        )

    plugins_dir.mkdir(parents=True, exist_ok=True)

    # Open zip in memory.
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise InstallError(f"bundle is not a valid zip file: {exc}") from exc

    # Locate plugin.json without extracting (peek).
    try:
        plugin_json_raw = zf.read("plugin.json")
    except KeyError:
        raise InstallError("bundle is missing plugin.json at the top level")

    try:
        plugin_json_data = _json.loads(plugin_json_raw)
    except _json.JSONDecodeError as exc:
        raise InstallError(f"plugin.json is not valid JSON: {exc}") from exc

    try:
        plugin = PluginJSON.model_validate(plugin_json_data)
    except Exception as exc:
        msg = str(exc)[:1024]
        raise InstallError(f"plugin.json failed schema validation: {msg}") from exc

    # Derive an internal directory name from plugin.name (operator never sees this).
    existing = list_installed_slugs(plugins_dir)
    internal_name = slugify(plugin.name, existing)

    staging = plugins_dir / f"{internal_name}.installing"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()

    try:
        _safe_extract(zf, staging)

        # Synthesize a runtime.yaml for backward compat with the existing
        # loader's RuntimeConfig model. Plugins start disabled.
        runtime = RuntimeConfig(
            plugin=plugin.name,
            enabled=False,
            package_index=0 if plugin.runtime.mode in ("registry", "bundled") else None,
            remote_index=0 if plugin.runtime.mode == "remote" else None,
        )
        save_runtime(staging, runtime)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    final = plugins_dir / internal_name
    if final.exists():
        # Race or operator double-install — clean up staging and bail.
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallError(f"plugin {plugin.name!r} already installed")

    staging.rename(final)
    return final


def list_installed_slugs(plugins_dir: Path) -> set[str]:
    """Set of currently-installed plugin directory names (for collision
    suffix)."""
    if not plugins_dir.exists():
        return set()
    return {
        d.name for d in plugins_dir.iterdir()
        if d.is_dir() and not d.name.endswith(".installing")
        and not d.name.startswith(".")
    }
```

Re-export from `glados/plugins/__init__.py`:

```python
from .store import install_from_zip
# ... append to __all__:
    "install_from_zip",
```

Run: `python -m pytest tests/test_plugins_zip_install.py -v` → 10 passed.

- [ ] **Step 5: Loader fallback — prefer plugin.json, fall back to server.json**

Modify `glados/plugins/loader.py:load_plugin`:

```python
def load_plugin(plugin_dir: Path) -> Plugin:
    """Load and validate a single plugin directory.

    v2 path: read plugin.json directly.
    v1 fallback: read server.json + synthesize via v1_to_v2.

    Raises ManifestError on any validation failure."""
    plugin_json_path = plugin_dir / "plugin.json"
    server_json_path = plugin_dir / "server.json"
    runtime = load_runtime(plugin_dir)

    if plugin_json_path.exists():
        # v2 native path
        try:
            raw = json.loads(plugin_json_path.read_text(encoding="utf-8"))
            from .bundle import PluginJSON
            manifest_v2 = PluginJSON.model_validate(raw)
        except Exception as exc:
            raise ManifestError(
                f"plugin.json in {plugin_dir} failed validation: {exc}"
            ) from exc
        secrets = load_secrets(plugin_dir)
        return Plugin(
            directory=plugin_dir,
            manifest_v2=manifest_v2,
            manifest=None,  # v1 not used
            runtime=runtime,
            secrets=secrets,
        )

    if not server_json_path.exists():
        raise ManifestError(
            f"plugin in {plugin_dir} has neither plugin.json nor server.json"
        )

    # v1 fallback
    try:
        raw = json.loads(server_json_path.read_text(encoding="utf-8"))
        manifest = ServerJSON.model_validate(raw)
    except Exception as exc:
        raise ManifestError(
            f"server.json in {plugin_dir} failed validation: {exc}"
        ) from exc

    if not manifest.packages and not manifest.remotes:
        raise ManifestError(
            f"server.json in {plugin_dir} has neither packages nor remotes"
        )

    if runtime.plugin != manifest.name:
        raise ManifestError(
            f"runtime.yaml.plugin ({runtime.plugin!r}) does not match server.json.name "
            f"({manifest.name!r}) in {plugin_dir}"
        )

    if runtime.package_index is None and runtime.remote_index is None:
        raise ManifestError(
            f"runtime.yaml in {plugin_dir} must set either package_index or remote_index"
        )

    secrets = load_secrets(plugin_dir)
    from .bundle import v1_to_v2
    manifest_v2 = v1_to_v2(
        raw,
        package_index=runtime.package_index,
        remote_index=runtime.remote_index,
    )

    return Plugin(
        directory=plugin_dir,
        manifest_v2=manifest_v2,
        manifest=manifest,
        runtime=runtime,
        secrets=secrets,
    )
```

Update the `Plugin` dataclass to carry `manifest_v2`:

```python
@dataclass(frozen=True)
class Plugin:
    directory: Path
    manifest_v2: "PluginJSON"  # always present (v1 plugins synthesize)
    manifest: ServerJSON | None  # v1 only; None for v2-native installs
    runtime: RuntimeConfig
    secrets: dict[str, str]

    @property
    def name(self) -> str:
        return self.manifest_v2.name
```

(The runner and serializers must adapt to read from `manifest_v2`. Update them in step 6.)

- [ ] **Step 6: Update runner.py for the three modes + WebUI serializers**

In `glados/plugins/runner.py:plugin_to_mcp_config`:

```python
def plugin_to_mcp_config(plugin: Plugin) -> MCPServerConfig:
    rt = plugin.manifest_v2.runtime
    if rt.mode == "remote":
        return _build_remote_v2(plugin, rt)
    if rt.mode == "registry":
        return _build_registry_v2(plugin, rt)
    if rt.mode == "bundled":
        return _build_bundled_v2(plugin, rt)
    raise ManifestError(f"unsupported runtime mode {rt.mode!r}")


def _build_remote_v2(plugin: Plugin, rt) -> MCPServerConfig:
    headers = _resolve_settings(plugin, only_headers=True)
    return MCPServerConfig(
        name=plugin.name,
        transport="http",
        url=str(rt.url),
        headers=headers or None,
    )


def _build_registry_v2(plugin: Plugin, rt) -> MCPServerConfig:
    runtime_hint, _, pkg_with_ver = rt.package.partition(":")
    if runtime_hint not in ("uvx", "npx"):
        raise ManifestError(f"unsupported registry runtime {runtime_hint}")
    args = [pkg_with_ver]
    cache_dir = plugin.directory / ".uvx-cache"
    if runtime_hint == "uvx":
        args.extend(["--cache-dir", str(cache_dir)])
    env = _resolve_settings(plugin, only_env=True)
    if runtime_hint == "npx":
        env["npm_config_cache"] = str(cache_dir)
    return MCPServerConfig(
        name=plugin.name,
        transport="stdio",
        command=runtime_hint,
        args=args,
        env=env or None,
    )


def _build_bundled_v2(plugin: Plugin, rt) -> MCPServerConfig:
    env = _resolve_settings(plugin, only_env=True)
    # cwd via env GLADOS_PLUGIN_DIR so the spawned script can find its files
    env["GLADOS_PLUGIN_DIR"] = str(plugin.directory)
    return MCPServerConfig(
        name=plugin.name,
        transport="stdio",
        command=rt.command,
        args=list(rt.args),
        env=env or None,
    )


def _resolve_settings(plugin: Plugin, *, only_env: bool = False, only_headers: bool = False) -> dict[str, str]:
    """Merge runtime.yaml.env_values + secrets.env, applying defaults."""
    out: dict[str, str] = {}
    for setting in plugin.manifest_v2.settings:
        # secrets win on collision
        value = plugin.secrets.get(setting.key) or plugin.runtime.env_values.get(setting.key)
        if value is None and setting.default is not None:
            value = str(setting.default)
        if value is None and setting.is_required:
            raise ManifestError(
                f"plugin {plugin.name} requires setting {setting.label!r} "
                f"(set it in plugin configuration)"
            )
        if value is not None:
            out[setting.key] = value
    return out
```

Update `glados/webui/plugin_endpoints.py` serializers to read from `manifest_v2`:

```python
def serialize_plugin_summary(plugin) -> dict:
    m = plugin.manifest_v2
    return {
        "name": m.name,
        "internal_name": plugin.directory.name,
        "title": m.name,
        "version": m.version,
        "description": m.description,
        "category": m.category,
        "icon": m.icon or "plug",
        "enabled": plugin.enabled,
        "is_remote": m.runtime.mode == "remote",
    }


def serialize_plugin_detail(plugin) -> dict:
    m = plugin.manifest_v2
    secrets_masked = {k: SECRET_PLACEHOLDER for k in plugin.secrets}
    return {
        "name": m.name,
        "internal_name": plugin.directory.name,
        "manifest": m.model_dump(mode="json"),
        "runtime": plugin.runtime.model_dump(mode="json"),
        "secrets": secrets_masked,
        "is_remote": m.runtime.mode == "remote",
    }
```

The endpoint URL slug stays as `<internal_name>` (the directory name); the
operator never sees it because the WebUI keys plugin tabs by `name` and
hands `internal_name` only on internal API calls.

Run all plugin tests after this step: `python -m pytest tests/test_plugins_*.py tests/test_webui_plugins.py -v` — debug until green.

- [ ] **Step 7: Add `POST /api/plugins/upload` handler + remove `/install`**

In `glados/webui/tts_ui.py:_dispatch_plugins_post`, replace the `/install` branch:

```python
def _dispatch_plugins_post(self) -> None:
    path = self.path
    if path == "/api/plugins/upload":
        self._upload_plugin()
        return
    if path == "/api/plugins/indexes":
        self._set_plugin_indexes()
        return

    rest = path[len("/api/plugins/"):]
    if rest.endswith("/enable"):
        self._set_plugin_enabled(rest[:-len("/enable")], True)
        return
    if rest.endswith("/disable"):
        self._set_plugin_enabled(rest[:-len("/disable")], False)
        return
    self._save_plugin_runtime(rest)
```

Add `_upload_plugin`:

```python
def _upload_plugin(self) -> None:
    """Multipart upload of a v2 plugin zip bundle."""
    content_length = int(self.headers.get("Content-Length", "0"))
    if content_length > 50 * 1024 * 1024:
        self._send_json(400, {"error": "bundle too large (max 50 MB)"})
        return
    if content_length == 0:
        self._send_json(400, {"error": "empty upload"})
        return

    content_type = self.headers.get("Content-Type", "")
    if not content_type.startswith("multipart/form-data"):
        self._send_json(400, {"error": "expected multipart/form-data"})
        return

    # Parse multipart, extract the 'bundle' file part.
    import cgi
    import io as _io
    fs = cgi.FieldStorage(
        fp=self.rfile,
        headers=self.headers,
        environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type},
    )
    if "bundle" not in fs:
        self._send_json(400, {"error": "missing 'bundle' file field"})
        return
    bundle_field = fs["bundle"]
    if not getattr(bundle_field, "file", None):
        self._send_json(400, {"error": "'bundle' must be a file upload"})
        return
    zip_bytes = bundle_field.file.read()

    plugins_dir = _plugins.default_plugins_dir()
    try:
        final_dir = _plugins.install_from_zip(zip_bytes, plugins_dir)
    except _plugins.InstallError as exc:
        self._send_json(400, {"error": str(exc)})
        return
    except Exception as exc:
        logger.warning("plugin upload failed: {}", exc)
        self._send_json(500, {"error": "upload failed"})
        return

    # Re-load the freshly installed plugin to return its detail to the WebUI.
    try:
        plugin = _plugins.load_plugin(final_dir)
    except Exception as exc:
        self._send_json(500, {"error": f"installed but cannot load: {exc}"})
        return

    self._send_json(200, _plugins.serialize_plugin_detail(plugin))
```

Re-export `install_from_zip` and remove the public surface for `install_from_url` from `glados.webui.plugin_endpoints` (keep the helper in the module but stop exposing the route).

Run: `python -m pytest tests/test_webui_plugins.py -v`. Existing install-by-URL tests will fail; either delete them or adapt to upload (preferred — adapt the happy-path one to upload-zip, delete the SSRF/redirect-bypass tests since they no longer apply).

- [ ] **Step 8: Run the full suite + commit**

```bash
python -m pytest -q
```
Expected: all green, suite size approximately 1575 + ~25 new bundle tests + ~10 new zip-install tests - ~6 deleted install-by-URL tests = ~1604.

Commit:
```bash
git add glados/plugins/bundle.py \
        glados/plugins/loader.py \
        glados/plugins/runner.py \
        glados/plugins/store.py \
        glados/plugins/__init__.py \
        glados/webui/plugin_endpoints.py \
        glados/webui/tts_ui.py \
        tests/test_plugins_bundle.py \
        tests/test_plugins_zip_install.py \
        tests/test_webui_plugins.py
git commit -m "feat(plugins): v2 bundle format — plugin.json + zip upload

Plugin install pivots from server.json-fetch-by-URL to a self-contained
zip bundle. The bundle has plugin.json (operator-facing manifest), an
optional server.json (v1 fallback), an optional README, an optional
icon, and optional bundled source.

Three runtime modes declared in plugin.json:
- registry: uvx/npx fetches the package at spawn time
- bundled:  spawn from inside the unpacked bundle (any GitHub MCP server
  zipped up alongside a plugin.json works without upstream cooperation)
- remote:   connect via streamable-HTTP

Operator-facing labels everywhere (settings[].label, not env-var keys).
Server-side internal directory name derived from name; never displayed.

POST /api/plugins/upload accepts multipart zip uploads with full safety
guards: 50 MB compressed cap, 200 MB uncompressed cap, 50 MB per-entry
cap, no symlinks, no path traversal, no absolute paths.

POST /api/plugins/install (the v1 URL-fetch endpoint) is removed.

Loader transparently handles v1 server.json-only installs via a
synthetic v1_to_v2 conversion so the WebUI form renderer sees a
single shape regardless of bundle vintage.

Spec: docs/superpowers/specs/2026-04-29-plugin-bundle-format-design.md"
```

---

## Task 2: WebUI rework — Upload tab, page-conformance, terminology pass

**Goal:** Drop the Add-by-URL inline section. Add an Upload section (drag-drop + file picker + progress). Form rendering uses `setting.label`, not the key. Category title-case via label map. Page-conformance pass: extract conventions from Memory / SSL / Logs / Raw YAML, apply them; drop bespoke `.plugins-page` / `.plugin-header-card` classes.

**Files:**
- Modify: `glados/webui/static/ui.js`
- Modify: `glados/webui/static/style.css`

**Acceptance Criteria:**
- [ ] Plugins page top-level layout matches the existing Configuration sub-page convention (audit Memory, SSL, Logs, Raw YAML to confirm — same header style, page intro, card spacing, button placement).
- [ ] Manage tab no longer contains an Add-by-URL section.
- [ ] Manage tab has an Upload section (drag-drop zone + file picker + progress + result message). Acceptable file: `.zip` only.
- [ ] Upload posts multipart with field name `bundle` to `POST /api/plugins/upload`. On success: refresh the page (rebuild tabs to include the new plugin) and switch to that plugin's tab.
- [ ] Browse tab unchanged in layout — but the gallery's Install button posts to `/api/plugins/upload` after fetching the bundle URL (or to `/api/plugins/upload` with a fetched zip).
- [ ] Form rendering reads from `manifest.settings[]` (v2 shape via the synthetic conversion). Each form field uses `setting.label` as the form label, NOT the key. The key is invisible.
- [ ] Category badges display title-case via a JS label map: `media → Media`, `home → Home`, `system → System`, `dev → Developer`, `utility → Utility`. Unknown categories fall back to JS-side title-case of the literal string.
- [ ] No "slug" / "manifest" / "runtime.yaml" / "secrets.env" / env-var keys visible anywhere in the UI.

**Verify:** Manual UI inspection on deploy. No automated test.

**Steps:**

- [ ] **Step 1: Audit existing Configuration sub-pages**

Discovery commands:
```bash
grep -n "function load.*Page\|function load.*Tab" glados/webui/static/ui.js | head -20
grep -n "config\.memory\|config\.ssl\|config\.logs\|config\.raw" glados/webui/static/ui.js
```

Read these page renderers in full:
- `loadMemoryPage` / `renderMemoryPage` / etc.
- `loadSslPage`
- `loadLogsPage`
- `loadRawYamlPage`

Extract the shared conventions:
- Page wrapper element (likely a `<div class="page-content">` or similar — note the actual class).
- Header pattern (heading element, intro paragraph, any right-aligned controls).
- Card pattern (existing `.card` class — verify spacing tokens).
- Tab strip pattern (used by System page — find the actual class names).
- Form field patterns (existing input + label + description grid layout).

Write down the extracted conventions in this task's commit message.

- [ ] **Step 2: Rebuild `loadPluginsPage` using extracted conventions**

Replace the existing bespoke `.plugins-page` / `.plugin-header-card` etc. with the conventions from Step 1. Specifically:
- Use the same outer element class as Memory/SSL/Logs/etc.
- Header pattern matches.
- Per-plugin tab content uses `.card` blocks (one for header info, one for Configuration form, one for Logs).
- Tab strip uses the existing class names from System page (likely `.page-tab-bar`, `.page-tab`, etc. — discover and reuse).

Drop these CSS rules (no longer used):
- `.plugins-page` (replaced by the standard page wrapper)
- `.plugin-header-card` (replaced by `.card`)
- `.page-tabs` if it's bespoke (use the existing tab-strip class)

Keep these CSS rules (genuinely plugin-specific):
- `.plugin-list` and `.plugin-row` (the Manage tab's installed-list rendering)
- `.plugin-cat-badge` (category badge styling)
- `.plugin-status-dot` and `.dot-on/.dot-off/.dot-err`
- `.plugin-switch` / `.plugin-slider` (the toggle styling)

- [ ] **Step 3: Replace Add-by-URL with Upload**

Locate `renderAddByUrlCard` and `wireAddByUrlHandlers` in ui.js (T10). Replace with `renderUploadCard` and `wireUploadHandlers`:

```javascript
function renderUploadCard() {
  return '' +
    '<div class="card">' +
    '  <div class="section-title">Install plugin</div>' +
    '  <div class="mode-desc" style="margin-bottom:10px;">' +
    '    Upload a plugin bundle (.zip). The bundle must contain <code>plugin.json</code> at the top level.' +
    '  </div>' +
    '  <div class="upload-dropzone" data-role="upload-dropzone">' +
    '    <div class="upload-prompt">Drop a .zip here, or <button class="btn-secondary" data-role="upload-pick">choose file</button></div>' +
    '    <input type="file" data-role="upload-input" accept=".zip,application/zip" hidden>' +
    '  </div>' +
    '  <div data-role="upload-result" class="install-result"></div>' +
    '</div>';
}

function wireUploadHandlers() {
  const dropzone = document.querySelector('[data-role="upload-dropzone"]');
  const fileInput = document.querySelector('[data-role="upload-input"]');
  const pickBtn = document.querySelector('[data-role="upload-pick"]');
  const resultEl = document.querySelector('[data-role="upload-result"]');
  if (!dropzone) return;

  pickBtn.addEventListener('click', (ev) => { ev.preventDefault(); fileInput.click(); });
  fileInput.addEventListener('change', () => {
    if (fileInput.files.length) handleUpload(fileInput.files[0]);
  });

  dropzone.addEventListener('dragover', (ev) => { ev.preventDefault(); dropzone.classList.add('dragover'); });
  dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
  dropzone.addEventListener('drop', (ev) => {
    ev.preventDefault();
    dropzone.classList.remove('dragover');
    if (ev.dataTransfer.files.length) handleUpload(ev.dataTransfer.files[0]);
  });

  async function handleUpload(file) {
    if (!file.name.toLowerCase().endsWith('.zip')) {
      resultEl.textContent = 'File must be a .zip bundle';
      resultEl.style.color = 'var(--red)';
      return;
    }
    if (file.size > 50 * 1024 * 1024) {
      resultEl.textContent = 'Bundle too large (max 50 MB)';
      resultEl.style.color = 'var(--red)';
      return;
    }
    resultEl.textContent = 'Uploading...';
    resultEl.style.color = '';

    const fd = new FormData();
    fd.append('bundle', file);
    try {
      const r = await fetch('/api/plugins/upload', {
        method: 'POST',
        credentials: 'same-origin',
        body: fd,
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
      resultEl.textContent = '';
      showToast('Plugin installed: ' + data.name, 'success');
      _pluginActiveSlug = data.internal_name;
      await loadPluginsPage();
    } catch (e) {
      resultEl.textContent = 'Install failed: ' + e.message;
      resultEl.style.color = 'var(--red)';
    }
  }
}
```

CSS for the dropzone:

```css
.upload-dropzone {
  border: 2px dashed var(--border-default);
  border-radius: var(--r-card);
  padding: var(--sp-5);
  text-align: center;
  background: var(--bg-input);
  transition: background 0.15s, border-color 0.15s;
}
.upload-dropzone.dragover {
  background: var(--bg-card);
  border-color: var(--orange);
}
.upload-prompt {
  color: var(--fg-secondary);
  font-size: 0.9rem;
}
```

Browse tab Install button: change from POST to `/api/plugins/install` (gone) to fetch the bundle then POST upload:

```javascript
btn.addEventListener('click', async () => {
  const url = btn.getAttribute('data-bundle-url');
  if (!url) { showToast('Catalog entry missing bundle_url', 'error'); return; }
  btn.disabled = true; btn.textContent = 'Installing...';
  try {
    // Fetch the bundle, upload to GLaDOS.
    const bundleResp = await fetch(url);
    if (!bundleResp.ok) throw new Error('bundle fetch HTTP ' + bundleResp.status);
    const blob = await bundleResp.blob();
    const fd = new FormData();
    fd.append('bundle', blob, 'bundle.zip');
    const r = await fetch('/api/plugins/upload', {
      method: 'POST', credentials: 'same-origin', body: fd,
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    showToast('Installed: ' + data.name, 'success');
    _pluginActiveSlug = data.internal_name;
    await loadPluginsPage();
  } catch (e) {
    showToast('Install failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Install';
  }
});
```

- [ ] **Step 4: Form labels from `setting.label`, not key**

In `renderConfigForm` and `renderFormField` (T8): the data shape is now
the v2 synthetic. The form iterates `manifest.settings[]` not
`packages[i].environmentVariables[]`. Each setting:
- Label: `setting.label` (operator-friendly).
- Input id: `setting.key` (internal — used in form serialization, not shown).
- Type widget: `setting.type` (text → text, url → url, number → number, boolean → checkbox, select → select w/ choices, secret → password).
- Required: `setting.required`.
- Default: `setting.default` (placeholder only).
- Description: `setting.description` (small text below).

The save flow stays unchanged: POSTs `{env_values, header_values, arg_values, secrets}` keyed by setting.key — server-side merge handles persistence.

- [ ] **Step 5: Category label map**

In ui.js, near the top:

```javascript
const _PLUGIN_CATEGORY_LABELS = {
  'media': 'Media',
  'home': 'Home',
  'integrations': 'Integrations',
  'system': 'System',
  'dev': 'Developer',
  'utility': 'Utility',
};

function pluginCategoryLabel(cat) {
  return _PLUGIN_CATEGORY_LABELS[cat] || (cat ? cat.charAt(0).toUpperCase() + cat.slice(1) : 'Other');
}
```

Use `pluginCategoryLabel(cat)` everywhere a category is rendered (tab badges, list rows, gallery cards).

- [ ] **Step 6: Terminology sweep**

Grep ui.js for operator-visible strings containing:
- "slug", "Slug" — replace with category-appropriate term or remove.
- "manifest" — replace with "plugin info" or contextual phrasing.
- "runtime.yaml" / "secrets.env" — should not appear in any user-visible string. Remove.

Also sweep error messages in plugin_endpoints.py and tts_ui.py for the same terms in 4xx/5xx response bodies.

- [ ] **Step 7: Run pytest + commit**

```bash
python -m pytest -q
```

```bash
git add glados/webui/static/ui.js glados/webui/static/style.css
git commit -m "refactor(webui): plugins UI conforms to design system; drops Add-by-URL for Upload

- Page layout uses the existing Configuration sub-page conventions
  (header / .card / tab strip from Memory / SSL / Logs / Raw YAML).
  Bespoke .plugins-page / .plugin-header-card classes removed.

- Add-by-URL inline section replaced with Upload (drag-drop + file
  picker, .zip only, 50 MB cap). POSTs multipart to
  /api/plugins/upload introduced in the prior commit.

- Form labels source from setting.label (operator-friendly) instead
  of the env-var key. Internal keys are no longer visible.

- Category labels rendered via a label map (media → Media, etc).

- Terminology sweep: slug / manifest / runtime.yaml / secrets.env
  no longer leak to operator-visible strings."
```

---

## Task 3: Docs

**Goal:** New `docs/plugin-bundle-format.md` (operator-facing schema reference + "wrap any MCP server in 5 minutes" guide). Architecture doc gains the v1→v2 migration note. CHANGES Change 33 entry. README updates the install flow walkthrough.

**Files:**
- Create: `docs/plugin-bundle-format.md`
- Modify: `docs/plugins-architecture.md`
- Modify: `docs/CHANGES.md`
- Modify: `README.md`

**Acceptance Criteria:**
- [ ] `plugin-bundle-format.md` covers: bundle layout, plugin.json schema (with example), three runtime modes with one example each, settings types table, "wrap any MCP server" tutorial.
- [ ] Architecture doc has a "v2 bundle format" section + migration note (existing v1 server.json-only installs continue to work via fallback).
- [ ] CHANGES Change 33 follows Change 32's structure.
- [ ] README install walkthrough mentions Browse + Upload (no Add-by-URL).

**Verify:** `git diff --stat` shows only the 4 doc files.

**Steps:** Standard docs writing — no special TDD pattern. Commit message:

```
docs(plugins): v2 bundle format guide + architecture migration + Change 33

New docs/plugin-bundle-format.md covers the bundle layout, plugin.json
schema, three runtime modes (registry / bundled / remote), and a step-
by-step "wrap any MCP server" tutorial.

Architecture doc gains a v2 section and notes that v1 server.json-only
installs continue to load via the loader's fallback path.

CHANGES.md Change 33 covers the full v1 → v2 migration.

README install flow updated: Browse / Upload only; Add-by-URL gone.
```

---

## Task 4: Deploy + verify

**Goal:** Push branch + redeploy via `scripts/_local_deploy.py`. Verify health, confirm new endpoint `POST /api/plugins/upload` reachable, confirm `/api/plugins/install` removed.

**Steps:**

- [ ] `git push origin webui-polish`
- [ ] `MSYS_NO_PATHCONV=1 GLADOS_SSH_HOST=docker-host.local ... python scripts/_local_deploy.py`
- [ ] Verify via paramiko: container healthy, image SHA captured, `POST /api/plugins/upload` returns 401 unauth, `POST /api/plugins/install` returns 404 unauth (or 401-then-404 if it falls through the dispatcher).
- [ ] Update `C:\src\SESSION_STATE.md` Active Handoff with the new image SHA.

---

## Self-Review Checklist (run after writing the plan)

- **Spec coverage:** zip format → T1; plugin.json schema → T1; runtime modes → T1; UI rework → T2; terminology → T2; backward compat → T1 (loader fallback); docs → T3.
- **No placeholders.**
- **Type consistency:** `PluginJSON`, `Setting`, `RegistryRuntime`, `BundledRuntime`, `RemoteRuntime`, `install_from_zip`, `v1_to_v2` consistent throughout.
- **Files-touched list matches per-task acceptance criteria.**

## Open after this plan

- Plugin signing / verification (v3).
- Auto-update on bundle version change.
- Plugin marketplace / catalog UI improvements.
