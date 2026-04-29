"""Convert a loaded :class:`Plugin` into the existing
:class:`glados.mcp.config.MCPServerConfig` so the existing
``MCPManager`` can spin up the plugin's tools without changes.

Mapping rules:

* ``remotes[].type == "streamable-http"`` → ``MCPServerConfig.transport == "http"``
  (the manager's existing identifier for streamable-HTTP transport).
* ``remotes[].type == "sse"`` → ``transport == "sse"``.
* ``packages[].transport.type == "stdio"`` → ``transport == "stdio"``,
  with ``command`` derived from the package's ``runtimeHint`` and
  ``args`` from its ``identifier`` + ``packageArguments``.

Subprocess spawning behavior for stdio plugins (the actual ``uvx`` /
``npx`` invocation) is handled by ``MCPManager.start()`` once it
receives the ``MCPServerConfig`` — this module just produces the
config. The ``runtimeHint`` field of the manifest is what tells us
which executable to invoke.
"""
from __future__ import annotations

from glados.mcp.config import MCPServerConfig

from .errors import ManifestError
from .loader import Plugin
from .manifest import InputArgument, Package, Remote


_RUNTIME_COMMANDS: dict[str, str] = {
    "uvx": "uvx",
    "npx": "npx",
    "dnx": "dnx",
}


def plugin_to_mcp_config(plugin: Plugin) -> MCPServerConfig:
    """Translate a :class:`Plugin` to an :class:`MCPServerConfig`.

    Raises :class:`ManifestError` if the selected package or remote
    can't be expressed as an MCPServerConfig (e.g. a stdio package
    without a usable ``runtimeHint``)."""
    if plugin.is_remote():
        return _build_remote_config(plugin)
    return _build_local_config(plugin)


# ── Remote (HTTP/SSE) ────────────────────────────────────────────────


def _build_remote_config(plugin: Plugin) -> MCPServerConfig:
    assert plugin.runtime.remote_index is not None
    remote: Remote = plugin.manifest.remotes[plugin.runtime.remote_index]

    transport = "http" if remote.type == "streamable-http" else "sse"

    # Resolve URL templating against runtime.yaml.arg_values + secrets +
    # header_values. Spec lets remote URLs reference {variableName} from
    # remotes[].variables. We do a simple {{key}}/{key} replacement.
    url = _expand_template(remote.url, plugin.runtime.arg_values)

    # Headers: combine non-secret runtime.header_values with secret
    # overrides from secrets.env. secrets.env wins on key collision.
    headers: dict[str, str] = {}
    for header in remote.headers:
        value = plugin.secrets.get(header.name) or plugin.runtime.header_values.get(header.name)
        if value is None and header.default is not None:
            value = header.default
        if value is None and header.is_required:
            raise ManifestError(
                f"plugin {plugin.name} requires header {header.name!r} (set it in "
                f"runtime.yaml.header_values or secrets.env)"
            )
        if value is not None:
            headers[header.name] = value

    return MCPServerConfig(
        name=plugin.name,
        transport=transport,
        url=url,
        headers=headers or None,
    )


# ── Local (stdio) ────────────────────────────────────────────────────


def _build_local_config(plugin: Plugin) -> MCPServerConfig:
    assert plugin.runtime.package_index is not None
    package: Package = plugin.manifest.packages[plugin.runtime.package_index]

    if package.transport.type != "stdio":
        # remotes[] is the right home for HTTP-transport plugins.
        # If a package[] entry says streamable-http, it means the
        # package itself runs an HTTP server locally; we don't
        # support that shape yet — surface the gap explicitly.
        raise ManifestError(
            f"plugin {plugin.name}: packages[{plugin.runtime.package_index}].transport "
            f"is {package.transport.type!r}; only 'stdio' packages are supported in "
            "Phase 2 scaffolding (use remotes[] for HTTP-transport plugins)"
        )

    if not package.runtime_hint:
        raise ManifestError(
            f"plugin {plugin.name}: packages[{plugin.runtime.package_index}] missing "
            "runtimeHint — required for stdio packages so we know how to invoke "
            "the binary (uvx / npx / dnx)"
        )

    if package.runtime_hint not in _RUNTIME_COMMANDS:
        raise ManifestError(
            f"plugin {plugin.name}: unsupported runtimeHint {package.runtime_hint!r} "
            f"(supported: {sorted(_RUNTIME_COMMANDS)})"
        )

    command = _RUNTIME_COMMANDS[package.runtime_hint]
    args = _build_stdio_args(package, plugin)
    env = _resolve_env(package, plugin)

    return MCPServerConfig(
        name=plugin.name,
        transport="stdio",
        command=command,
        args=args,
        env=env or None,
    )


def _build_stdio_args(package: Package, plugin: Plugin) -> list[str]:
    """Build the argv list for an stdio plugin.

    For uvx: ``[<pkg>@<ver>, --cache-dir, <plugin>/.uvx-cache, ...packageArguments]``
    For npx: ``[<pkg>@<ver>, ...packageArguments]`` (cache via env, see _resolve_env)

    Runtime arguments (e.g. Docker mounts) are skipped here — they only
    apply when the package is actually a Docker image (registryType=oci),
    which we don't run in-process today."""
    # The package identifier with version — runtime selects the right tool.
    # uvx accepts ``--from <pkg>==<version> <entrypoint>`` style; the
    # simpler ``<pkg>@<version>`` form works for most cases.
    args: list[str] = [f"{package.identifier}@{package.version}"]

    # Per-plugin cache routing. Phase 2b: uvx accepts --cache-dir as a CLI flag.
    # npx ignores it and uses the npm_config_cache env var instead (see _resolve_env).
    if package.runtime_hint == "uvx":
        cache_dir = plugin.directory / ".uvx-cache"
        args.extend(["--cache-dir", str(cache_dir)])

    for arg in package.package_arguments:
        rendered = _render_argument(arg, plugin)
        if rendered:
            args.extend(rendered)

    return args


def _render_argument(arg: InputArgument, plugin: Plugin) -> list[str]:
    """Resolve a single ``packageArguments[]`` entry into argv tokens."""
    value = plugin.runtime.arg_values.get(arg.name or "") if arg.name else None
    if value is None:
        value = arg.value
    if value is None:
        value = arg.default
    if value is None:
        if arg.is_required:
            raise ManifestError(
                f"plugin {plugin.name} requires argument {arg.name or arg.value_hint!r} "
                "(set it in runtime.yaml.arg_values)"
            )
        return []

    if arg.type == "named":
        if not arg.name:
            raise ManifestError(
                f"plugin {plugin.name}: named argument missing name"
            )
        return [arg.name, value]
    return [value]  # positional


def _resolve_env(package: Package, plugin: Plugin) -> dict[str, str]:
    """Merge runtime.yaml.env_values + secrets.env, applying defaults
    from server.json for any unset env. Raise on missing required envs."""
    env: dict[str, str] = {}
    for ev in package.environment_variables:
        value = plugin.secrets.get(ev.name) or plugin.runtime.env_values.get(ev.name)
        if value is None and ev.default is not None:
            value = ev.default
        if value is None and ev.is_required:
            raise ManifestError(
                f"plugin {plugin.name} requires env {ev.name!r} (set it in "
                f"runtime.yaml.env_values or secrets.env)"
            )
        if value is not None:
            env[ev.name] = value

    # Phase 2b: npx honors npm_config_cache to redirect its cache dir.
    # uvx uses --cache-dir CLI flag instead (see _build_stdio_args).
    if package.runtime_hint == "npx":
        env["npm_config_cache"] = str(plugin.directory / ".uvx-cache")

    return env


# ── helpers ─────────────────────────────────────────────────────────


def _expand_template(template: str, values: dict[str, str]) -> str:
    """Replace ``{key}`` placeholders in a remote URL template."""
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", val)
    return out
