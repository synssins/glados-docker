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
from pathlib import Path

import yaml

from .errors import ManifestError
from .manifest import RuntimeConfig


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
