"""install_plugin / remove_plugin / set_enabled / slugify."""
from __future__ import annotations

from pathlib import Path

import pytest

from glados.plugins.errors import InstallError
from glados.plugins.manifest import ServerJSON
from glados.plugins.store import (
    install_plugin,
    load_runtime,
    remove_plugin,
    set_enabled,
    slugify,
)


def test_slugify_simple_name():
    assert slugify("mcp-arr", set()) == "mcp-arr"
    assert slugify("MCP-ARR", set()) == "mcp-arr"


def test_slugify_reverse_dns_takes_last_segment():
    assert slugify("io.github.aplaceforallmystuff/mcp-arr", set()) == "mcp-arr"


def test_slugify_collision_appends_suffix():
    existing = {"mcp-arr"}
    assert slugify("mcp-arr", existing) == "mcp-arr-2"
    existing.add("mcp-arr-2")
    assert slugify("mcp-arr", existing) == "mcp-arr-3"


def test_slugify_strips_non_alphanumeric():
    assert slugify("foo bar!@#baz", set()) == "foo-bar-baz"


def _manifest(name: str = "demo.python", local: bool = True) -> ServerJSON:
    raw = {
        "name": name,
        "description": "demo",
        "version": "0.1.0",
    }
    if local:
        raw["packages"] = [{
            "registryType": "pypi",
            "identifier": "demo-mcp",
            "version": "1.0.0",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
        }]
    else:
        raw["remotes"] = [{"type": "streamable-http", "url": "https://x.test/mcp"}]
    return ServerJSON.model_validate(raw)


def test_install_plugin_creates_directory_with_files(tmp_path: Path):
    install_plugin(tmp_path, "demo", _manifest())
    assert (tmp_path / "demo" / "server.json").exists()
    assert (tmp_path / "demo" / "runtime.yaml").exists()


def test_install_plugin_stub_runtime_disabled_and_correct_index(tmp_path: Path):
    install_plugin(tmp_path, "demo-local", _manifest(local=True))
    rt = load_runtime(tmp_path / "demo-local")
    assert rt.enabled is False
    assert rt.package_index == 0
    assert rt.remote_index is None

    install_plugin(tmp_path, "demo-remote", _manifest("demo.remote", local=False))
    rt = load_runtime(tmp_path / "demo-remote")
    assert rt.enabled is False
    assert rt.remote_index == 0
    assert rt.package_index is None


def test_install_plugin_refuses_existing_dir(tmp_path: Path):
    install_plugin(tmp_path, "demo", _manifest())
    with pytest.raises(InstallError, match="already exists"):
        install_plugin(tmp_path, "demo", _manifest())


def test_remove_plugin_rmtree(tmp_path: Path):
    install_plugin(tmp_path, "demo", _manifest())
    assert (tmp_path / "demo").exists()
    remove_plugin(tmp_path, "demo")
    assert not (tmp_path / "demo").exists()


def test_remove_plugin_missing_is_noop(tmp_path: Path):
    remove_plugin(tmp_path, "not-there")  # no raise


def test_remove_plugin_refuses_path_outside_plugins_dir(tmp_path: Path):
    with pytest.raises(InstallError, match="outside"):
        remove_plugin(tmp_path, "../escape")


def test_set_enabled_round_trip(tmp_path: Path):
    install_plugin(tmp_path, "demo", _manifest())
    plugin_dir = tmp_path / "demo"

    rt = set_enabled(plugin_dir, True)
    assert rt.enabled is True
    rt2 = load_runtime(plugin_dir)
    assert rt2.enabled is True

    rt = set_enabled(plugin_dir, False)
    assert rt.enabled is False
