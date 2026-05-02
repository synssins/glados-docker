"""Convert a loaded :class:`Plugin` into the existing
:class:`glados.mcp.config.MCPServerConfig` so the existing
``MCPManager`` can spin up the plugin's tools without changes.

Mapping rules (all driven by ``plugin.manifest_v2.runtime.mode``):

* ``remote`` -> ``MCPServerConfig.transport == "http"`` (streamable-HTTP);
  url + headers from the synthesized PluginJSON.
* ``registry`` -> ``transport == "stdio"`` with ``command`` derived from
  the package's runtime hint (``uvx`` / ``npx`` / ``dnx``) and ``args``
  from the package's identifier@version.
* ``bundled`` -> ``transport == "stdio"`` with ``command`` and ``args``
  copied verbatim from the manifest. ``GLADOS_PLUGIN_DIR`` is exported
  so the spawned script can locate its own files.

Per-runtime cache routing (uvx ``--cache-dir`` flag, npx
``npm_config_cache`` env) is applied for ``registry`` mode.
"""
from __future__ import annotations

from glados.mcp.config import MCPServerConfig

from .errors import ManifestError
from .loader import Plugin


_REGISTRY_COMMANDS: dict[str, str] = {
    "uvx": "uvx",
    "npx": "npx",
    "dnx": "dnx",
}


def plugin_to_mcp_config(plugin: Plugin) -> MCPServerConfig:
    """Translate a :class:`Plugin` to an :class:`MCPServerConfig`."""
    rt = plugin.manifest_v2.runtime
    if rt.mode == "remote":
        return _build_remote_v2(plugin, rt)
    if rt.mode == "registry":
        return _build_registry_v2(plugin, rt)
    if rt.mode == "bundled":
        return _build_bundled_v2(plugin, rt)
    raise ManifestError(f"unsupported runtime mode {rt.mode!r}")


# ── Remote (streamable-HTTP) ─────────────────────────────────────────


def _build_remote_v2(plugin: Plugin, rt) -> MCPServerConfig:
    # URL templating: v1 plugins (synthesized) carry templates like
    # ``https://{ha_host}/api/mcp`` and rely on
    # ``runtime.yaml.arg_values`` for substitution. Preserve the
    # behavior so v1-on-disk installs keep working.
    url = _expand_template(str(rt.url), plugin.runtime.arg_values)
    headers = _resolve_settings(plugin)
    return MCPServerConfig(
        name=plugin.name,
        transport="http",
        url=url,
        headers=headers or None,
    )


# ── Registry (uvx / npx / dnx) ───────────────────────────────────────


def _build_registry_v2(plugin: Plugin, rt) -> MCPServerConfig:
    runtime_hint, _, pkg_with_ver = rt.package.partition(":")
    if runtime_hint not in _REGISTRY_COMMANDS:
        raise ManifestError(
            f"plugin {plugin.name}: unsupported runtime {runtime_hint!r} "
            f"(supported: {sorted(_REGISTRY_COMMANDS)})"
        )

    args: list[str] = [pkg_with_ver]
    cache_dir = plugin.directory / ".uvx-cache"
    if runtime_hint == "uvx":
        # uvx accepts --cache-dir as a CLI flag.
        args.extend(["--cache-dir", str(cache_dir)])

    env = _resolve_settings(plugin)
    if runtime_hint == "npx":
        # npx ignores --cache-dir; use the npm_config_cache env var.
        env["npm_config_cache"] = str(cache_dir)

    return MCPServerConfig(
        name=plugin.name,
        transport="stdio",
        command=_REGISTRY_COMMANDS[runtime_hint],
        args=args,
        env=env or None,
    )


# ── Bundled (run from inside the unpacked bundle) ────────────────────


def _build_bundled_v2(plugin: Plugin, rt) -> MCPServerConfig:
    env = _resolve_settings(plugin)
    # Subprocess can locate its own files via this pinned env var.
    env["GLADOS_PLUGIN_DIR"] = str(plugin.directory)
    return MCPServerConfig(
        name=plugin.name,
        transport="stdio",
        command=rt.command,
        args=list(rt.args),
        env=env or None,
    )


# ── Settings resolution (env values + secrets + defaults) ────────────


def _resolve_settings(plugin: Plugin) -> dict[str, str]:
    """Merge ``runtime.yaml.env_values``/``header_values`` + ``secrets.env``
    against the v2 settings list, applying defaults and raising on
    missing required values."""
    out: dict[str, str] = {}
    runtime = plugin.runtime
    # v1 used separate env_values and header_values; v2 collapses both
    # under a single settings list. Look in both buckets so v1-on-disk
    # plugins (synthesized to v2) still find their stored values.
    for setting in plugin.manifest_v2.settings:
        value = (
            plugin.secrets.get(setting.key)
            or runtime.env_values.get(setting.key)
            or runtime.header_values.get(setting.key)
        )
        if value is None and setting.default is not None:
            value = str(setting.default)
        if value is None and setting.is_required:
            raise ManifestError(
                f"plugin {plugin.name} requires setting {setting.label!r} "
                "(set it in plugin configuration)"
            )
        if value is not None:
            out[setting.key] = value
    return out


# ── helpers ─────────────────────────────────────────────────────────


def _expand_template(template: str, values: dict[str, str]) -> str:
    """Replace ``{key}`` placeholders in a remote URL template."""
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", val)
    return out
