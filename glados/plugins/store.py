"""Persistence helpers for plugin runtime state.

``runtime.yaml`` — non-secret values (env, headers, arg values) +
   enable flag + package selector. YAML, human-readable.
``secrets.env`` — KEY=VALUE per line, mode 0600. Loaded at spawn time,
   merged onto the env passed to the subprocess (or as Authorization
   headers for remote plugins).

This module is the only place that reads/writes either file.
"""
from __future__ import annotations

import os
import re
import shutil
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
