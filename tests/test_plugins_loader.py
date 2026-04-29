"""Tests for plugin discovery / loading and conversion to MCPServerConfig.

Drops fake plugin directories under a tmp_path, calls
``discover_plugins`` and ``plugin_to_mcp_config``, and checks that the
existing MCP infra would be fed the right thing.
"""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from glados.plugins import (
    ManifestError,
    discover_plugins,
    load_plugin,
    plugin_to_mcp_config,
)


# ── Fixtures: build a fake plugins dir on disk ─────────────────────────


def _write_plugin(
    plugins_dir: Path,
    name: str,
    server_json: dict,
    runtime_yaml: str,
    secrets: dict[str, str] | None = None,
) -> Path:
    plugin_dir = plugins_dir / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "server.json").write_text(json.dumps(server_json), encoding="utf-8")
    (plugin_dir / "runtime.yaml").write_text(runtime_yaml, encoding="utf-8")
    if secrets:
        body = "".join(f"{k}={v}\n" for k, v in secrets.items())
        (plugin_dir / "secrets.env").write_text(body, encoding="utf-8")
    return plugin_dir


REMOTE_HA = {
    "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
    "name": "io.home-assistant/assist",
    "description": "HA assist API",
    "version": "1.0.0",
    "remotes": [
        {
            "type": "streamable-http",
            "url": "https://{ha_host}/api/mcp",
            "headers": [
                {
                    "name": "Authorization",
                    "isRequired": True,
                    "isSecret": True,
                }
            ],
        }
    ],
}


STDIO_ARR = {
    "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
    "name": "io.github.aplaceforallmystuff/mcp-arr",
    "description": "*arr stack",
    "version": "1.4.2",
    "packages": [
        {
            "registryType": "pypi",
            "identifier": "mcp-arr",
            "version": "1.4.2",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
            "environmentVariables": [
                {"name": "SONARR_URL", "default": "http://sonarr:8989"},
                {"name": "SONARR_API_KEY", "isRequired": True, "isSecret": True},
            ],
        }
    ],
}


# ── Discovery & load_plugin ─────────────────────────────────────────


def test_discover_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    out = discover_plugins(tmp_path / "does-not-exist")
    assert out == []


def test_discover_loads_remote_plugin(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        "ha",
        REMOTE_HA,
        textwrap.dedent("""
            plugin: io.home-assistant/assist
            remote_index: 0
            arg_values:
              ha_host: "ha.local:8123"
        """).strip(),
        secrets={"Authorization": "Bearer xxx.yyy.zzz"},
    )
    plugins = discover_plugins(tmp_path)
    assert len(plugins) == 1
    p = plugins[0]
    assert p.name == "io.home-assistant/assist"
    assert p.is_remote() is True
    assert p.secrets["Authorization"] == "Bearer xxx.yyy.zzz"


def test_discover_loads_local_stdio_plugin(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        "mcp-arr",
        STDIO_ARR,
        textwrap.dedent("""
            plugin: io.github.aplaceforallmystuff/mcp-arr
            package_index: 0
            env_values:
              SONARR_URL: "http://sonarr.lan:8989"
        """).strip(),
        secrets={"SONARR_API_KEY": "secret-key"},
    )
    plugins = discover_plugins(tmp_path)
    assert len(plugins) == 1
    p = plugins[0]
    assert p.is_remote() is False
    assert p.runtime.env_values["SONARR_URL"] == "http://sonarr.lan:8989"


def test_disabled_plugin_skipped(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        "off",
        STDIO_ARR,
        textwrap.dedent("""
            plugin: io.github.aplaceforallmystuff/mcp-arr
            enabled: false
            package_index: 0
        """).strip(),
    )
    assert discover_plugins(tmp_path) == []


def test_broken_plugin_does_not_block_others(tmp_path: Path) -> None:
    # First plugin: server.json is invalid JSON
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "server.json").write_text("{ broken json", encoding="utf-8")
    (bad / "runtime.yaml").write_text("plugin: x\nremote_index: 0\n", encoding="utf-8")

    # Second plugin: valid
    _write_plugin(
        tmp_path,
        "good",
        REMOTE_HA,
        "plugin: io.home-assistant/assist\nremote_index: 0\n",
        secrets={"Authorization": "Bearer x"},
    )

    plugins = discover_plugins(tmp_path)
    assert [p.name for p in plugins] == ["io.home-assistant/assist"]


def test_runtime_name_mismatch_rejected(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        "mismatch",
        REMOTE_HA,
        "plugin: NOT-THE-RIGHT-NAME\nremote_index: 0\n",
        secrets={"Authorization": "Bearer x"},
    )
    # discover swallows the error; load_plugin raises directly.
    with pytest.raises(ManifestError):
        load_plugin(tmp_path / "mismatch")


def test_runtime_index_out_of_range(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        "oob",
        STDIO_ARR,
        textwrap.dedent("""
            plugin: io.github.aplaceforallmystuff/mcp-arr
            package_index: 99
        """).strip(),
    )
    with pytest.raises(ManifestError):
        load_plugin(tmp_path / "oob")


def test_no_install_method_rejected(tmp_path: Path) -> None:
    bad = dict(REMOTE_HA)
    bad["remotes"] = []
    _write_plugin(
        tmp_path,
        "naked",
        bad,
        "plugin: io.home-assistant/assist\nremote_index: 0\n",
    )
    with pytest.raises(ManifestError):
        load_plugin(tmp_path / "naked")


def test_dot_directories_skipped(tmp_path: Path) -> None:
    (tmp_path / ".uvx-cache").mkdir()
    _write_plugin(
        tmp_path,
        "real",
        REMOTE_HA,
        "plugin: io.home-assistant/assist\nremote_index: 0\n",
        secrets={"Authorization": "Bearer x"},
    )
    plugins = discover_plugins(tmp_path)
    assert [p.name for p in plugins] == ["io.home-assistant/assist"]


# ── Runner: plugin_to_mcp_config ───────────────────────────────────


def test_remote_plugin_to_http_config(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        "ha",
        REMOTE_HA,
        textwrap.dedent("""
            plugin: io.home-assistant/assist
            remote_index: 0
            arg_values:
              ha_host: "ha.local:8123"
        """).strip(),
        secrets={"Authorization": "Bearer xxx"},
    )
    plugin = load_plugin(tmp_path / "ha")
    cfg = plugin_to_mcp_config(plugin)
    assert cfg.transport == "http"
    assert str(cfg.url).startswith("https://ha.local:8123/api/mcp")
    assert cfg.headers["Authorization"] == "Bearer xxx"


def test_local_stdio_plugin_to_uvx_config(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        "mcp-arr",
        STDIO_ARR,
        textwrap.dedent("""
            plugin: io.github.aplaceforallmystuff/mcp-arr
            package_index: 0
        """).strip(),
        secrets={"SONARR_API_KEY": "key"},
    )
    plugin = load_plugin(tmp_path / "mcp-arr")
    cfg = plugin_to_mcp_config(plugin)
    assert cfg.transport == "stdio"
    assert cfg.command == "uvx"
    assert cfg.args[0] == "mcp-arr@1.4.2"
    assert cfg.env["SONARR_API_KEY"] == "key"
    assert cfg.env["SONARR_URL"] == "http://sonarr:8989"  # default applied


def test_missing_required_secret_raises(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        "noauth",
        STDIO_ARR,
        "plugin: io.github.aplaceforallmystuff/mcp-arr\npackage_index: 0\n",
        # No SONARR_API_KEY in secrets — but it's marked isRequired.
    )
    plugin = load_plugin(tmp_path / "noauth")
    with pytest.raises(ManifestError):
        plugin_to_mcp_config(plugin)


def test_plugins_dir_env_override(tmp_path: Path, monkeypatch) -> None:
    custom = tmp_path / "custom-plugins"
    custom.mkdir()
    monkeypatch.setenv("GLADOS_PLUGINS_DIR", str(custom))
    from glados.plugins.loader import default_plugins_dir
    assert default_plugins_dir() == custom
