"""Discover and load plugins from ``/app/data/plugins/``.

Each subdirectory MUST contain a ``server.json`` (manifest) and SHOULD
contain a ``runtime.yaml`` (operator-resolved values). Optional
``secrets.env`` carries ``isSecret: true`` env values.

Plugins missing required files or failing validation are SKIPPED with
a logged warning — never raised — so a single broken plugin doesn't
prevent the others from loading.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from .errors import ManifestError
from .manifest import RuntimeConfig, ServerJSON
from .store import load_runtime, load_secrets


def default_plugins_dir() -> Path:
    """``GLADOS_DATA/plugins/`` by default. Operator can override with
    ``GLADOS_PLUGINS_DIR`` env."""
    override = os.environ.get("GLADOS_PLUGINS_DIR", "").strip()
    if override:
        return Path(override)
    data = Path(os.environ.get("GLADOS_DATA", "/app/data"))
    return data / "plugins"


@dataclass(frozen=True)
class Plugin:
    """A loaded plugin — manifest + runtime config + resolved secrets.

    ``directory`` is the on-disk plugin folder (the ``runtime.yaml``
    parent). ``secrets`` is the merged ``secrets.env`` content
    (already resolved, ready to pass to subprocess.env or HTTP
    headers — never log this).
    """

    directory: Path
    manifest: ServerJSON
    runtime: RuntimeConfig
    secrets: dict[str, str]

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def enabled(self) -> bool:
        return self.runtime.enabled

    def is_remote(self) -> bool:
        """True iff this plugin is configured to use a ``remotes[]``
        entry (HTTP/SSE), not a local ``packages[]`` entry."""
        return self.runtime.remote_index is not None


def load_plugin(plugin_dir: Path) -> Plugin:
    """Load and validate a single plugin directory. Raises
    :class:`ManifestError` on any validation failure."""
    server_json_path = plugin_dir / "server.json"
    if not server_json_path.exists():
        raise ManifestError(f"server.json missing in {plugin_dir}")

    try:
        raw = json.loads(server_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"server.json in {plugin_dir} not valid JSON: {exc}"
        ) from exc

    try:
        manifest = ServerJSON.model_validate(raw)
    except Exception as exc:
        raise ManifestError(
            f"server.json in {plugin_dir} failed schema validation: {exc}"
        ) from exc

    if not manifest.packages and not manifest.remotes:
        raise ManifestError(
            f"server.json in {plugin_dir} has neither packages[] nor remotes[] — "
            "at least one install method required"
        )

    runtime = load_runtime(plugin_dir)

    if runtime.plugin != manifest.name:
        raise ManifestError(
            f"runtime.yaml.plugin ({runtime.plugin!r}) does not match "
            f"server.json.name ({manifest.name!r}) in {plugin_dir}"
        )

    if runtime.package_index is None and runtime.remote_index is None:
        raise ManifestError(
            f"runtime.yaml in {plugin_dir} must set either package_index or remote_index"
        )
    if runtime.package_index is not None and runtime.remote_index is not None:
        raise ManifestError(
            f"runtime.yaml in {plugin_dir} sets BOTH package_index and remote_index — pick one"
        )
    if runtime.package_index is not None and runtime.package_index >= len(manifest.packages):
        raise ManifestError(
            f"runtime.yaml.package_index={runtime.package_index} is out of range "
            f"(server.json has {len(manifest.packages)} packages)"
        )
    if runtime.remote_index is not None and runtime.remote_index >= len(manifest.remotes):
        raise ManifestError(
            f"runtime.yaml.remote_index={runtime.remote_index} is out of range "
            f"(server.json has {len(manifest.remotes)} remotes)"
        )

    secrets = load_secrets(plugin_dir)

    return Plugin(
        directory=plugin_dir,
        manifest=manifest,
        runtime=runtime,
        secrets=secrets,
    )


def discover_plugins(plugins_dir: Path | None = None) -> list[Plugin]:
    """Walk ``plugins_dir`` (defaults to ``/app/data/plugins/``) and
    return every plugin that loaded cleanly. Broken plugins are logged
    and skipped — never raised — so one malformed manifest doesn't
    take down the rest."""
    plugins_dir = plugins_dir or default_plugins_dir()
    if not plugins_dir.exists():
        logger.info("Plugins directory {!s} does not exist; skipping discovery", plugins_dir)
        return []

    out: list[Plugin] = []
    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue  # skip .uvx-cache etc. at the top level
        try:
            plugin = load_plugin(entry)
        except ManifestError as exc:
            logger.warning("Plugin {!s} skipped: {}", entry.name, exc)
            continue
        if not plugin.enabled:
            logger.info("Plugin {!s} disabled in runtime.yaml; skipping", plugin.name)
            continue
        out.append(plugin)
        logger.success(
            "Plugin loaded: {} v{} ({}category={})",
            plugin.name, plugin.manifest.version,
            "remote, " if plugin.is_remote() else "local, ",
            plugin.manifest.glados_category,
        )
    return out
