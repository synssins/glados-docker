"""PluginJSON schema, runtime modes, settings, v1->v2 conversion."""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def _minimal(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "name": "Demo Plugin",
        "description": "A demo plugin.",
        "version": "1.0.0",
        "category": "utility",
        "runtime": {"mode": "registry", "package": "uvx:demo-mcp@1.0.0"},
    }
    base.update(overrides)
    return base


def test_minimal_plugin_json_parses():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal())
    assert p.name == "Demo Plugin"
    assert p.runtime.mode == "registry"
    assert p.runtime.package == "uvx:demo-mcp@1.0.0"


def test_runtime_registry_requires_package():
    from glados.plugins.bundle import PluginJSON
    with pytest.raises(ValidationError, match="package"):
        PluginJSON.model_validate(_minimal(runtime={"mode": "registry"}))


def test_runtime_bundled_requires_command_and_args():
    from glados.plugins.bundle import PluginJSON
    with pytest.raises(ValidationError, match="command|args"):
        PluginJSON.model_validate(_minimal(runtime={"mode": "bundled"}))


def test_runtime_bundled_with_command():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal(
        runtime={"mode": "bundled", "command": "node", "args": ["src/index.js"]}
    ))
    assert p.runtime.command == "node"
    assert p.runtime.args == ["src/index.js"]


def test_runtime_remote_requires_https_url():
    from glados.plugins.bundle import PluginJSON
    with pytest.raises(ValidationError, match="https"):
        PluginJSON.model_validate(_minimal(
            runtime={"mode": "remote", "url": "http://x.test/mcp"}
        ))


def test_runtime_remote_with_https_url():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal(
        runtime={"mode": "remote", "url": "https://x.test/mcp"}
    ))
    assert str(p.runtime.url) == "https://x.test/mcp"


def test_settings_text_default():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal(settings=[
        {"key": "FOO", "label": "Foo", "type": "text"}
    ]))
    assert p.settings[0].label == "Foo"
    assert p.settings[0].is_required is False


def test_settings_select_requires_choices():
    from glados.plugins.bundle import PluginJSON
    with pytest.raises(ValidationError, match="choices"):
        PluginJSON.model_validate(_minimal(settings=[
            {"key": "Q", "label": "Q", "type": "select"}
        ]))


def test_settings_secret_type_accepted():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal(settings=[
        {"key": "API_KEY", "label": "API Key", "type": "secret", "required": True}
    ]))
    assert p.settings[0].type == "secret"
    assert p.settings[0].is_required is True


def test_category_unknown_string_accepted_as_literal():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal(category="custom-bucket"))
    assert p.category == "custom-bucket"


def test_v1_server_json_to_v2_conversion_remote():
    """v1 server.json with remotes[] -> synthetic plugin.json with mode=remote."""
    from glados.plugins.bundle import v1_to_v2
    server_json = {
        "name": "io.example/demo",
        "description": "demo",
        "version": "1.0.0",
        "remotes": [{
            "type": "streamable-http",
            "url": "https://x.test/mcp",
            "headers": [{"name": "Authorization", "isRequired": True, "isSecret": True}],
        }],
    }
    p = v1_to_v2(server_json, package_index=None, remote_index=0)
    assert p.runtime.mode == "remote"
    assert str(p.runtime.url) == "https://x.test/mcp"
    assert p.settings[0].key == "Authorization"
    assert p.settings[0].type == "secret"


def test_intent_keywords_default_empty():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal())
    assert p.intent_keywords == []


def test_intent_keywords_lowercased_and_stripped():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal(
        intent_keywords=["Movie", "  TV  ", "TORRENT", ""]
    ))
    # Empty / whitespace-only entries are dropped; survivors lower-cased.
    assert p.intent_keywords == ["movie", "tv", "torrent"]


def test_v1_server_json_to_v2_conversion_registry():
    """v1 server.json with packages[uvx] -> synthetic plugin.json with mode=registry."""
    from glados.plugins.bundle import v1_to_v2
    server_json = {
        "name": "demo.python",
        "description": "demo",
        "version": "1.0.0",
        "packages": [{
            "registryType": "pypi",
            "identifier": "demo-mcp",
            "version": "1.0.0",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
            "environmentVariables": [
                {"name": "DEMO_KEY", "isRequired": True, "isSecret": True}
            ],
        }],
    }
    p = v1_to_v2(server_json, package_index=0, remote_index=None)
    assert p.runtime.mode == "registry"
    assert p.runtime.package == "uvx:demo-mcp@1.0.0"
    assert p.settings[0].type == "secret"
