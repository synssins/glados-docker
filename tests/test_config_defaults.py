"""Stage 3 Phase 6 — Commit 2 coverage.

Locks in the same-stack mDNS / docker-service-name default URLs and
verifies that env vars still win over pydantic defaults, YAML still
parses backward-compatibly, and YAML-set deprecated fields emit a
loguru warning so operators know to clean their config files.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import pytest
from loguru import logger

from glados.core.config_store import (
    AuditGlobal,
    GlobalConfig,
    HomeAssistantGlobal,
    MemoryConfig,
    NetworkGlobal,
    PathsGlobal,
    ServicesConfig,
    TuningGlobal,
    WeatherGlobal,
)


@contextmanager
def _capture_warnings() -> Iterator[list[str]]:
    """Capture loguru WARNING-level messages into a list for assertions."""
    messages: list[str] = []
    handler_id = logger.add(
        lambda msg: messages.append(str(msg)),
        level="WARNING",
        format="{message}",
    )
    try:
        yield messages
    finally:
        logger.remove(handler_id)


@contextmanager
def _env(**kwargs: str) -> Iterator[None]:
    """Temporarily set env vars, restoring the prior state on exit."""
    old: dict[str, str | None] = {k: os.environ.get(k) for k in kwargs}
    try:
        for k, v in kwargs.items():
            os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── Default URL values ─────────────────────────────────────────────────


def test_ha_defaults_use_mdns() -> None:
    ha = HomeAssistantGlobal()
    assert ha.url == "http://homeassistant.local:8123"
    assert ha.ws_url == "ws://homeassistant.local:8123/api/websocket"
    assert ha.token == ""


def test_services_defaults_use_docker_service_names() -> None:
    s = ServicesConfig()
    assert s.ollama_interactive.url == "http://ollama:11434"
    assert s.ollama_autonomy.url == "http://ollama:11434"
    assert s.ollama_vision.url == "http://ollama:11434"
    assert s.tts.url == "http://speaches:8800"
    assert s.stt.url == "http://speaches:8800"
    assert s.vision.url == "http://glados-vision:8016"


def test_memory_defaults_use_docker_service_name() -> None:
    m = MemoryConfig()
    assert m.chromadb_host == "chromadb"
    assert m.chromadb_port == 8000


# ── Env overrides ──────────────────────────────────────────────────────


def test_ha_env_wins_over_yaml_placeholder() -> None:
    # Operators put a placeholder token in YAML; the real token arrives
    # via HA_TOKEN env. The model_validator flips the default precedence.
    with _env(HA_TOKEN="real-token-xyz", HA_URL="http://ha.example:8123"):
        ha = HomeAssistantGlobal.model_validate({
            "url": "http://192.168.1.104:8123",
            "token": "eyJh...PLACEHOLDER",
        })
        assert ha.token == "real-token-xyz"
        assert ha.url == "http://ha.example:8123"


def test_services_yaml_url_still_wins_over_default() -> None:
    # Services do NOT use env-overrides-YAML (unlike HomeAssistantGlobal) —
    # this is intentional per the Phase 6 migration policy: operators with
    # legacy host-specific URLs in services.yaml keep working silently. A
    # later release may flip the precedence once YAML URLs are fully retired.
    s = ServicesConfig.model_validate({
        "ollama_interactive": {"url": "http://192.168.1.75:11434"},
    })
    assert s.ollama_interactive.url == "http://192.168.1.75:11434"
    # Untouched fields still resolve from pydantic defaults.
    assert s.tts.url == "http://speaches:8800"


# ── Backward compat: existing YAML still parses ────────────────────────


def test_existing_yaml_with_full_urls_still_parses() -> None:
    # Operators upgrading from pre-Phase-6 configs have hardcoded IPs
    # in services.yaml / global.yaml. Those must continue to work.
    legacy_services = ServicesConfig.model_validate({
        "ollama_interactive": {"url": "http://192.168.1.75:11434"},
        "tts": {"url": "http://192.168.1.75:5050", "voice": "glados"},
    })
    assert legacy_services.ollama_interactive.url == "http://192.168.1.75:11434"
    assert legacy_services.tts.url == "http://192.168.1.75:5050"

    legacy_global = GlobalConfig.model_validate({
        "home_assistant": {"url": "http://192.168.1.104:8123"},
    })
    assert legacy_global.home_assistant.url == "http://192.168.1.104:8123"


# ── Deprecation warnings ───────────────────────────────────────────────


@pytest.mark.parametrize("model_cls, yaml_payload, field_name", [
    (PathsGlobal, {"glados_root": "/x"}, "glados_root"),
    (PathsGlobal, {"audio_base": "/x"}, "audio_base"),
    (PathsGlobal, {"logs": "/x"}, "logs"),
    (PathsGlobal, {"data": "/x"}, "data"),
    (PathsGlobal, {"assets": "/x"}, "assets"),
    (NetworkGlobal, {"serve_host": "1.2.3.4"}, "serve_host"),
    (NetworkGlobal, {"serve_port": 9999}, "serve_port"),
    (AuditGlobal, {"path": "/x/a.jsonl"}, "path"),
    (AuditGlobal, {"retention_days": 90}, "retention_days"),
    (TuningGlobal, {"engine_audio_default": False}, "engine_audio_default"),
    (WeatherGlobal, {"temperature_unit": "celsius"}, "temperature_unit"),
    (WeatherGlobal, {"wind_speed_unit": "kmh"}, "wind_speed_unit"),
])
def test_deprecated_yaml_field_emits_warning(model_cls, yaml_payload, field_name) -> None:
    with _capture_warnings() as msgs:
        model_cls.model_validate(yaml_payload)
    joined = "\n".join(msgs)
    assert f"'{model_cls.__name__}.{field_name}' is deprecated" in joined, (
        f"Expected deprecation warning for {model_cls.__name__}.{field_name}; got: {joined!r}"
    )


def test_defaults_do_not_warn() -> None:
    # Instantiating a model with no YAML should be silent — operators
    # on fresh installs shouldn't see deprecation noise.
    with _capture_warnings() as msgs:
        PathsGlobal()
        NetworkGlobal()
        AuditGlobal()
        TuningGlobal()
        WeatherGlobal()
        ServicesConfig()
    assert msgs == [], f"Expected no warnings on default instantiation, got: {msgs!r}"


def test_services_gladys_api_deprecated_when_set_via_yaml() -> None:
    with _capture_warnings() as msgs:
        ServicesConfig.model_validate({"gladys_api": {"url": "http://localhost:8020"}})
    assert any("gladys_api" in m for m in msgs), msgs


# ── Phase 6 Commit 3: committed example YAML is stripped of deprecated
# fields and service URL defaults, so fresh installs rely on pydantic
# defaults + the WebUI for upstream URLs. Regression guard: if someone
# re-adds a deprecated field to the example, this test fails loudly.
# ────────────────────────────────────────────────────────────────────


def test_config_example_yaml_has_no_deprecated_fields() -> None:
    import yaml
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "configs" / "config.example.yaml"
    assert example.exists(), f"Missing {example}"
    data = yaml.safe_load(example.read_text(encoding="utf-8")) or {}

    # Every deprecated field that is scoped to a top-level YAML section.
    deprecated = {
        "paths": None,        # whole section
        "network": None,      # whole section
        ("audit", "path"): None,
        ("audit", "retention_days"): None,
        ("tuning", "engine_audio_default"): None,
        ("weather", "temperature_unit"): None,
        ("weather", "wind_speed_unit"): None,
        ("services", "gladys_api"): None,
    }
    violations: list[str] = []
    for key in deprecated:
        if isinstance(key, tuple):
            outer, inner = key
            if isinstance(data.get(outer), dict) and inner in data[outer]:
                violations.append(f"{outer}.{inner}")
        else:
            if key in data:
                violations.append(key)
    assert not violations, (
        f"config.example.yaml contains deprecated fields: {violations}. "
        "Remove them — operators relying on the example shouldn't be "
        "copying deprecated configuration into their config.yaml."
    )


def test_config_example_yaml_has_no_service_url_defaults() -> None:
    """Commit 3 stripped service URLs from the example; pydantic defaults
    drive fresh installs. Catch regressions where someone re-pins URLs."""
    import yaml
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "configs" / "config.example.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8")) or {}
    services = data.get("services") or {}

    pinned_urls: list[str] = []
    for svc_name, svc_cfg in services.items():
        if isinstance(svc_cfg, dict) and "url" in svc_cfg:
            pinned_urls.append(f"services.{svc_name}.url")
    assert not pinned_urls, (
        f"config.example.yaml pins service URLs: {pinned_urls}. "
        "Post-Phase-6 the example should rely on pydantic same-stack "
        "defaults; operators customize URLs via env or the WebUI."
    )
