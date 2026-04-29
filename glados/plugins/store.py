"""Persistence helpers for plugin runtime state.

``runtime.yaml`` — non-secret values (env, headers, arg values) +
   enable flag + package selector. YAML, human-readable.
``secrets.env`` — KEY=VALUE per line, mode 0600. Loaded at spawn time,
   merged onto the env passed to the subprocess (or as Authorization
   headers for remote plugins).

This module is the only place that reads/writes either file.
"""
from __future__ import annotations

import io
import json as _json
import os
import re
import shutil
import zipfile
from pathlib import Path

import yaml

from .errors import InstallError, ManifestError
from .manifest import RuntimeConfig


_SLUG_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_SLUG_VALID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def runtime_path(plugin_dir: Path) -> Path:
    return plugin_dir / "runtime.yaml"


def secrets_path(plugin_dir: Path) -> Path:
    return plugin_dir / "secrets.env"


def load_runtime(plugin_dir: Path) -> RuntimeConfig:
    """Read and validate ``runtime.yaml``. Raises ``ManifestError`` on
    parse/validation failure. Caller is expected to surface a
    plugin-friendly error message."""
    path = runtime_path(plugin_dir)
    if not path.exists():
        raise ManifestError(
            f"runtime.yaml missing in {plugin_dir} — create one or "
            "(re)install the plugin"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ManifestError(f"runtime.yaml in {plugin_dir} not valid YAML: {exc}") from exc
    try:
        return RuntimeConfig.model_validate(raw)
    except Exception as exc:
        raise ManifestError(
            f"runtime.yaml in {plugin_dir} failed validation: {exc}"
        ) from exc


def save_runtime(plugin_dir: Path, config: RuntimeConfig) -> None:
    """Write ``runtime.yaml`` atomically (write to .tmp then rename)."""
    path = runtime_path(plugin_dir)
    tmp = path.with_suffix(".yaml.tmp")
    data = config.model_dump(mode="json", exclude_none=False)
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


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

    (staging / "server.json").write_text(
        manifest.model_dump_json(by_alias=True, exclude_none=True, indent=2),
        encoding="utf-8",
    )

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


def load_secrets(plugin_dir: Path) -> dict[str, str]:
    """Read ``secrets.env`` (KEY=VALUE per line). Returns ``{}`` if the
    file doesn't exist. Skips blank lines and ``#`` comments. Does NOT
    parse shell-style escaping — values are taken verbatim after the
    first ``=``."""
    path = secrets_path(plugin_dir)
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value
    return out


def save_secrets(plugin_dir: Path, secrets: dict[str, str]) -> None:
    """Write ``secrets.env`` with mode 0600. Atomic rename to avoid
    partial reads. Empty mapping writes an empty file (not deletes it)
    so the mode bit stays applied."""
    path = secrets_path(plugin_dir)
    tmp = path.with_suffix(".env.tmp")
    body = "\n".join(f"{k}={v}" for k, v in secrets.items())
    if body:
        body += "\n"
    tmp.write_text(body, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # Windows / non-POSIX: mode bit is best-effort.
        pass
    tmp.replace(path)


# ── v2 zip-bundle install ────────────────────────────────────────────
#
# WHY here, not its own module: the zip path needs slugify(),
# save_runtime(), and the same on-disk layout this module already owns.
# Splitting across modules would force circular imports.

MAX_ZIP_BYTES = 50 * 1024 * 1024            # 50 MB compressed
MAX_TOTAL_UNCOMPRESSED = 200 * 1024 * 1024  # 200 MB total uncompressed
MAX_ENTRY_UNCOMPRESSED = 50 * 1024 * 1024   # 50 MB per entry


def list_installed_slugs(plugins_dir: Path) -> set[str]:
    """Set of currently-installed plugin directory names, used for the
    collision-suffix calculation in :func:`slugify`."""
    if not plugins_dir.exists():
        return set()
    return {
        d.name for d in plugins_dir.iterdir()
        if d.is_dir() and not d.name.endswith(".installing")
        and not d.name.startswith(".")
    }


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract zf into dest with traversal/symlink/size guards."""
    total = 0
    dest_abs = dest.resolve()
    for member in zf.infolist():
        # Reject symlinks (POSIX file-type 0o120000 in upper 16 of external_attr).
        ftype = (member.external_attr >> 16) & 0o170000
        if ftype == 0o120000:
            raise InstallError(
                f"zip contains a symlink ({member.filename!r}); refusing"
            )
        # Reject absolute paths.
        if member.filename.startswith("/") or member.filename.startswith("\\"):
            raise InstallError(
                f"zip member {member.filename!r} is an absolute path"
            )
        # Reject path traversal.
        try:
            target = (dest / member.filename).resolve()
        except (OSError, ValueError):
            raise InstallError(
                f"zip member {member.filename!r} resolves outside dest"
            )
        if dest_abs not in target.parents and target != dest_abs:
            raise InstallError(
                f"zip member {member.filename!r} escapes target dir"
            )
        # Reject oversize entries (per-entry + running total).
        if member.file_size > MAX_ENTRY_UNCOMPRESSED:
            raise InstallError(
                f"zip member {member.filename!r} too large "
                f"({member.file_size} bytes; max {MAX_ENTRY_UNCOMPRESSED})"
            )
        total += member.file_size
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise InstallError(
                f"zip total uncompressed size exceeds "
                f"{MAX_TOTAL_UNCOMPRESSED} bytes"
            )
    zf.extractall(dest)


def install_from_zip(zip_bytes: bytes, plugins_dir: Path) -> Path:
    """Install a v2 plugin bundle from raw zip bytes. Returns the final
    plugin directory (e.g. plugins_dir/demo-plugin/). Raises
    :class:`InstallError` on any validation failure."""
    # Import here to avoid a top-of-module cycle (bundle imports nothing
    # from store, but store importing bundle at module scope drags
    # pydantic into the hot path of every helper above).
    from .bundle import PluginJSON

    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise InstallError(
            f"bundle too large ({len(zip_bytes)} bytes; max {MAX_ZIP_BYTES})"
        )

    plugins_dir.mkdir(parents=True, exist_ok=True)

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise InstallError(f"bundle is not a valid zip file: {exc}") from exc

    # Peek plugin.json without extracting so we can fail before touching disk.
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
        raise InstallError(
            f"plugin.json failed schema validation: {msg}"
        ) from exc

    # Internal directory name (operator never sees this); collision-safe.
    existing = list_installed_slugs(plugins_dir)
    internal_name = slugify(plugin.name, existing)

    staging = plugins_dir / f"{internal_name}.installing"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()

    try:
        _safe_extract(zf, staging)

        # Synthesize a runtime.yaml so the existing loader's RuntimeConfig
        # checks still pass. Plugins start disabled until the operator
        # provides any required settings.
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
        # TOCTOU race only -- slugify already returned a non-colliding name.
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallError(f"plugin {plugin.name!r} already installed")

    staging.rename(final)
    return final
