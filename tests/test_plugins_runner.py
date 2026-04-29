"""Runner cache injection: uvx via --cache-dir flag, npx via env."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from glados.plugins.loader import load_plugin
from glados.plugins.runner import plugin_to_mcp_config


def _write_plugin(tmp_path: Path, slug: str, manifest: dict, runtime: dict) -> Path:
    plugin_dir = tmp_path / slug
    plugin_dir.mkdir()
    (plugin_dir / "server.json").write_text(__import__("json").dumps(manifest))
    (plugin_dir / "runtime.yaml").write_text(yaml.safe_dump(runtime))
    return plugin_dir


def _uvx_manifest(name: str = "demo.python") -> dict:
    return {
        "name": name,
        "description": "demo",
        "version": "0.1.0",
        "packages": [{
            "registryType": "pypi",
            "identifier": "demo-mcp",
            "version": "1.2.3",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
        }],
    }


def _npx_manifest(name: str = "demo.node") -> dict:
    return {
        "name": name,
        "description": "demo",
        "version": "0.1.0",
        "packages": [{
            "registryType": "npm",
            "identifier": "@demo/mcp",
            "version": "1.2.3",
            "runtimeHint": "npx",
            "transport": {"type": "stdio"},
        }],
    }


def test_uvx_injects_cache_dir_flag(tmp_path: Path):
    plugin_dir = _write_plugin(
        tmp_path, "demo",
        _uvx_manifest(),
        {"plugin": "demo.python", "package_index": 0},
    )
    plugin = load_plugin(plugin_dir)
    cfg = plugin_to_mcp_config(plugin)

    assert cfg.transport == "stdio"
    assert cfg.command == "uvx"
    assert cfg.args[0] == "demo-mcp@1.2.3"
    assert "--cache-dir" in cfg.args
    cache_idx = cfg.args.index("--cache-dir")
    assert cfg.args[cache_idx + 1] == str(plugin_dir / ".uvx-cache")


def test_npx_injects_npm_config_cache_env(tmp_path: Path):
    plugin_dir = _write_plugin(
        tmp_path, "demo",
        _npx_manifest(),
        {"plugin": "demo.node", "package_index": 0},
    )
    plugin = load_plugin(plugin_dir)
    cfg = plugin_to_mcp_config(plugin)

    assert cfg.transport == "stdio"
    assert cfg.command == "npx"
    assert cfg.args[0] == "@demo/mcp@1.2.3"
    assert "--cache-dir" not in cfg.args  # npx uses env, not flag
    assert cfg.env is not None
    assert cfg.env["npm_config_cache"] == str(plugin_dir / ".uvx-cache")


def test_uvx_cache_dir_appears_after_package_identifier(tmp_path: Path):
    """--cache-dir must come after the package@version arg so uvx parses it correctly."""
    plugin_dir = _write_plugin(
        tmp_path, "demo",
        _uvx_manifest(),
        {"plugin": "demo.python", "package_index": 0},
    )
    plugin = load_plugin(plugin_dir)
    cfg = plugin_to_mcp_config(plugin)
    pkg_idx = cfg.args.index("demo-mcp@1.2.3")
    cache_idx = cfg.args.index("--cache-dir")
    assert cache_idx > pkg_idx


def test_remote_plugin_unaffected(tmp_path: Path):
    """Remote plugins don't grow a cache flag."""
    manifest = {
        "name": "demo.remote",
        "description": "demo",
        "version": "0.1.0",
        "remotes": [{"type": "streamable-http", "url": "https://example.test/mcp"}],
    }
    plugin_dir = _write_plugin(
        tmp_path, "demo-remote",
        manifest,
        {"plugin": "demo.remote", "remote_index": 0},
    )
    plugin = load_plugin(plugin_dir)
    cfg = plugin_to_mcp_config(plugin)
    assert cfg.transport == "http"
    assert cfg.args == []
