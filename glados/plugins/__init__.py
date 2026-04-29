"""GLaDOS plugin layer.

A plugin is an MCP server that conforms to the official ``server.json``
manifest format (schema ``2025-12-11`` from the MCP Registry). GLaDOS
reads the manifest generically — no per-plugin code lives in this repo.

See ``docs/plugins-architecture.md`` for the full design.

Public API:
    discover_plugins(plugins_dir)
    load_plugin(plugin_dir)
    plugin_to_mcp_config(plugin)
    Plugin, ServerJSON, RuntimeConfig
    PluginError, ManifestError
"""
from __future__ import annotations

from .errors import PluginError, ManifestError, InstallError
from .manifest import (
    EnvironmentVariable,
    InputArgument,
    Package,
    Remote,
    RemoteHeader,
    RuntimeConfig,
    ServerJSON,
    Transport,
)
from .loader import Plugin, discover_plugins, load_plugin
from .runner import plugin_to_mcp_config
from .store import install_plugin, remove_plugin, set_enabled, slugify

__all__ = [
    "EnvironmentVariable",
    "InputArgument",
    "InstallError",
    "ManifestError",
    "Package",
    "Plugin",
    "PluginError",
    "Remote",
    "RemoteHeader",
    "RuntimeConfig",
    "ServerJSON",
    "Transport",
    "discover_plugins",
    "install_plugin",
    "load_plugin",
    "plugin_to_mcp_config",
    "remove_plugin",
    "set_enabled",
    "slugify",
]
