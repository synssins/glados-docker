"""Discover and load plugins from ``/app/data/plugins/``.

Each subdirectory MUST contain a manifest:

* v2 native: ``plugin.json`` (preferred).
* v1 fallback: ``server.json`` (kept loading for installs that pre-date
  the v2 bundle format).

Plus ``runtime.yaml`` (operator-resolved values) and optional
``secrets.env`` for secret values.

Plugins missing required files or failing validation are SKIPPED with
a logged warning -- never raised -- so a single broken plugin doesn't
prevent the others from loading.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from .errors import ManifestError
from .manifest import RuntimeConfig, ServerJSON
from .store import load_runtime, load_secrets

if TYPE_CHECKING:
    from .bundle import PluginJSON


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
    """A loaded plugin -- manifest + runtime config + resolved secrets.

    ``manifest_v2`` is always populated: v2 plugins parse it directly
    from ``plugin.json``; v1 plugins synthesize it via
    :func:`glados.plugins.bundle.v1_to_v2`. Consumers (runner,
    serializers) should always read from ``manifest_v2``.

    ``manifest`` is the raw v1 :class:`ServerJSON` and is only present
    for v1-on-disk installs (``None`` for v2-native installs). Reserved
    for v1-specific code paths (re-export, etc).
    """

    directory: Path
    manifest_v2: "PluginJSON"
    manifest: ServerJSON | None
    runtime: RuntimeConfig
    secrets: dict[str, str]

    @property
    def name(self) -> str:
        return self.manifest_v2.name

    @property
    def enabled(self) -> bool:
        return self.runtime.enabled

    def is_remote(self) -> bool:
        """True iff this plugin's runtime mode is 'remote'."""
        return self.manifest_v2.runtime.mode == "remote"


def load_plugin(plugin_dir: Path) -> Plugin:
    """Load and validate a single plugin directory.

    v2 path: read ``plugin.json`` directly.
    v1 fallback: read ``server.json`` + synthesize via :func:`v1_to_v2`.

    Raises :class:`ManifestError` on any validation failure."""
    plugin_json_path = plugin_dir / "plugin.json"
    server_json_path = plugin_dir / "server.json"
    runtime = load_runtime(plugin_dir)

    # ── v2 native ────────────────────────────────────────────────
    if plugin_json_path.exists():
        try:
            raw = json.loads(plugin_json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestError(
                f"plugin.json in {plugin_dir} not valid JSON: {exc}"
            ) from exc
        # Local import: bundle pulls in pydantic; loader is imported on
        # every WebUI request.
        from .bundle import PluginJSON
        try:
            manifest_v2 = PluginJSON.model_validate(raw)
        except Exception as exc:
            raise ManifestError(
                f"plugin.json in {plugin_dir} failed validation: {exc}"
            ) from exc
        secrets = load_secrets(plugin_dir)
        return Plugin(
            directory=plugin_dir,
            manifest_v2=manifest_v2,
            manifest=None,
            runtime=runtime,
            secrets=secrets,
        )

    # ── v1 fallback ──────────────────────────────────────────────
    if not server_json_path.exists():
        raise ManifestError(
            f"plugin in {plugin_dir} has neither plugin.json nor server.json"
        )

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
            f"server.json in {plugin_dir} has neither packages[] nor remotes[] -- "
            "at least one install method required"
        )

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
            f"runtime.yaml in {plugin_dir} sets BOTH package_index and remote_index -- pick one"
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

    from .bundle import v1_to_v2
    try:
        manifest_v2 = v1_to_v2(
            raw,
            package_index=runtime.package_index,
            remote_index=runtime.remote_index,
        )
    except Exception as exc:
        raise ManifestError(
            f"server.json in {plugin_dir} could not be converted to plugin.json: {exc}"
        ) from exc

    return Plugin(
        directory=plugin_dir,
        manifest_v2=manifest_v2,
        manifest=manifest,
        runtime=runtime,
        secrets=secrets,
    )


def discover_plugins(
    plugins_dir: Path | None = None,
    *,
    include_disabled: bool = False,
) -> list[Plugin]:
    """Walk ``plugins_dir`` (defaults to ``/app/data/plugins/``) and
    return every plugin that loaded cleanly. Broken plugins are logged
    and skipped -- never raised -- so one malformed manifest doesn't
    take down the rest.

    By default, disabled plugins are filtered out (the engine only spawns
    enabled ones). Pass ``include_disabled=True`` for the WebUI listing,
    which has to show disabled plugins so operators can configure and
    enable them."""
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
        if not plugin.enabled and not include_disabled:
            logger.info("Plugin {!s} disabled in runtime.yaml; skipping", plugin.name)
            continue
        out.append(plugin)
        logger.success(
            "Plugin loaded: {} v{} ({}{}category={})",
            plugin.name, plugin.manifest_v2.version,
            "remote, " if plugin.is_remote() else "local, ",
            "disabled, " if not plugin.enabled else "",
            plugin.manifest_v2.category,
        )
    return out
