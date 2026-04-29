"""Tests for the plugin manifest layer (server.json + runtime.yaml).

Validates:
- ServerJSON parses the official 2025-12-11 schema shapes (minimal
  example + complex example from the spec).
- environmentVariables/headers/arguments expose the form-rendering
  metadata (isRequired / isSecret / default / choices / format).
- _meta accessors return GLaDOS-namespace values with sensible
  defaults.
- RuntimeConfig validates and round-trips through YAML.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from glados.plugins import (
    EnvironmentVariable,
    Package,
    Remote,
    RemoteHeader,
    RuntimeConfig,
    ServerJSON,
)


# ── ServerJSON parsing ────────────────────────────────────────────────


MINIMAL = {
    "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
    "name": "io.example/minimal",
    "description": "A minimal MCP server example",
    "version": "1.0.0",
    "packages": [
        {
            "registryType": "npm",
            "identifier": "@example/minimal",
            "version": "1.0.0",
            "transport": {"type": "stdio"},
        }
    ],
}


MCP_ARR = {
    "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
    "name": "io.github.aplaceforallmystuff/mcp-arr",
    "title": "*arr Stack",
    "description": "Sonarr / Radarr / Lidarr",
    "version": "1.4.2",
    "repository": {
        "url": "https://github.com/aplaceforallmystuff/mcp-arr",
        "source": "github",
    },
    "packages": [
        {
            "registryType": "pypi",
            "identifier": "mcp-arr",
            "version": "1.4.2",
            "runtimeHint": "uvx",
            "transport": {"type": "stdio"},
            "environmentVariables": [
                {
                    "name": "SONARR_URL",
                    "description": "Sonarr base URL",
                    "default": "http://sonarr:8989",
                },
                {
                    "name": "SONARR_API_KEY",
                    "description": "Sonarr API key",
                    "isRequired": True,
                    "isSecret": True,
                },
            ],
        }
    ],
    "_meta": {
        "com.synssins.glados/category": "media",
        "com.synssins.glados/icon": "film",
        "com.synssins.glados/min_glados_version": "1.0.0",
    },
}


HA_REMOTE = {
    "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
    "name": "io.home-assistant/assist",
    "description": "Home Assistant Assist API",
    "version": "1.0.0",
    "remotes": [
        {
            "type": "streamable-http",
            "url": "https://{ha_host}/api/mcp",
            "headers": [
                {
                    "name": "Authorization",
                    "description": "Bearer LLAT",
                    "isRequired": True,
                    "isSecret": True,
                }
            ],
        }
    ],
}


def test_minimal_server_json_parses() -> None:
    s = ServerJSON.model_validate(MINIMAL)
    assert s.name == "io.example/minimal"
    assert s.version == "1.0.0"
    assert len(s.packages) == 1
    assert s.packages[0].transport.type == "stdio"
    assert s.remotes == []


def test_mcp_arr_full_shape_parses() -> None:
    s = ServerJSON.model_validate(MCP_ARR)
    assert s.title == "*arr Stack"
    assert len(s.packages) == 1
    pkg = s.packages[0]
    assert pkg.runtime_hint == "uvx"
    assert pkg.transport.type == "stdio"
    # environmentVariables expose the form-rendering metadata
    api_key = next(e for e in pkg.environment_variables if e.name == "SONARR_API_KEY")
    assert api_key.is_required is True
    assert api_key.is_secret is True
    sonarr_url = next(e for e in pkg.environment_variables if e.name == "SONARR_URL")
    assert sonarr_url.is_required is False
    assert sonarr_url.default == "http://sonarr:8989"


def test_glados_meta_accessors() -> None:
    s = ServerJSON.model_validate(MCP_ARR)
    assert s.glados_category == "media"
    assert s.glados_icon == "film"
    assert s.glados_min_version == "1.0.0"
    assert s.glados_persona_role == "both"  # default


def test_meta_defaults_when_missing() -> None:
    s = ServerJSON.model_validate(MINIMAL)
    assert s.glados_category == "utility"
    assert s.glados_icon == "plug"
    assert s.glados_min_version is None
    assert s.glados_persona_role == "both"


def test_meta_persona_role_invalid_falls_back() -> None:
    bad = dict(MCP_ARR)
    bad["_meta"] = {"com.synssins.glados/recommended_persona_role": "wrong"}
    s = ServerJSON.model_validate(bad)
    assert s.glados_persona_role == "both"


def test_remote_with_secret_header() -> None:
    s = ServerJSON.model_validate(HA_REMOTE)
    assert len(s.remotes) == 1
    remote = s.remotes[0]
    assert remote.type == "streamable-http"
    assert remote.url == "https://{ha_host}/api/mcp"
    auth = remote.headers[0]
    assert auth.is_secret is True
    assert auth.is_required is True


def test_extra_top_level_field_rejected() -> None:
    bad = dict(MINIMAL)
    bad["pretendItsValid"] = "no"
    with pytest.raises(Exception):
        ServerJSON.model_validate(bad)


# ── RuntimeConfig ─────────────────────────────────────────────────────


def test_runtime_config_yaml_round_trip(tmp_path: Path) -> None:
    rc = RuntimeConfig(
        plugin="mcp-arr",
        package_index=0,
        env_values={"SONARR_URL": "http://lan:8989"},
    )
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump(rc.model_dump(mode="json")), encoding="utf-8")
    reread = RuntimeConfig.model_validate(yaml.safe_load(path.read_text()))
    assert reread.plugin == "mcp-arr"
    assert reread.package_index == 0
    assert reread.env_values["SONARR_URL"] == "http://lan:8989"
    assert reread.enabled is True


def test_runtime_config_extra_fields_rejected() -> None:
    with pytest.raises(Exception):
        RuntimeConfig.model_validate(
            {"plugin": "x", "package_index": 0, "futureField": "no"}
        )
