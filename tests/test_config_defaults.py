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
    AuthGlobal,
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


def test_services_defaults_self_contained() -> None:
    """TTS and STT default to the container's own api_wrapper endpoint
    via the loopback-only internal port (always plain HTTP). The public
    8015 listener may be TLS-wrapped when a cert is mounted, but in-
    container callers don't validate against a public domain cert, so
    127.0.0.1:18015 (or whatever GLADOS_INTERNAL_API_PORT is) is the
    canonical path. Both endpoints are served in-process via the
    bundled VITS + CTC ONNX models. Ollama defaults remain Docker
    service names (external LLM is still a required dependency).
    Vision defaults to empty (optional)."""
    s = ServicesConfig()
    assert s.llm_interactive.url == "http://ollama:11434"
    assert s.llm_autonomy.url == "http://ollama:11434"
    assert s.llm_vision.url == "http://ollama:11434"
    assert s.tts.url == "http://127.0.0.1:18015"
    assert s.stt.url == "http://127.0.0.1:18015"
    assert s.vision.url == ""


def test_memory_defaults_use_embedded_chromadb() -> None:
    """ChromaDB is embedded via PersistentClient as of 2026-04-24; the
    legacy host/port fields are kept for back-compat with older
    operator YAMLs but default to empty (deprecated)."""
    m = MemoryConfig()
    assert m.chromadb_path == "/app/data/chromadb"
    # Legacy fields default to empty — still present so old YAMLs
    # carrying them don't fail validation.
    assert m.chromadb_host == ""
    assert m.chromadb_port == 0


# ── Source-of-truth precedence (WebUI > env) ──────────────────────────
# Policy (locked 2026-04-23): YAML is always authoritative. Env values
# ONLY seed pydantic field defaults on fresh install when YAML is
# missing the field entirely. Once the WebUI has saved a value, env
# updates have no effect — this prevents stale compose env vars from
# silently reverting WebUI-saved tokens on the next container reload.


def test_ha_yaml_wins_over_env_when_both_set() -> None:
    """HA_TOKEN env MUST NOT override a value present in YAML. Earlier
    behaviour inverted this and caused a live incident where the WebUI
    'Save' button silently did nothing after the operator rotated the
    HA long-lived token (2026-04-23)."""
    with _env(HA_TOKEN="stale-env-token", HA_URL="http://env.example:8123"):
        ha = HomeAssistantGlobal.model_validate({
            "url": "http://ha-from-yaml.example:8123",
            "token": "fresh-yaml-token",
        })
        assert ha.token == "fresh-yaml-token"
        assert ha.url == "http://ha-from-yaml.example:8123"


def test_ha_env_seeds_when_yaml_field_missing() -> None:
    """Fresh install contract: if YAML has never been saved, env values
    seed the defaults so the container boots with something sensible."""
    with _env(HA_TOKEN="seed-token", HA_URL="http://seed.example:8123"):
        ha = HomeAssistantGlobal.model_validate({})
        assert ha.token == "seed-token"
        assert ha.url == "http://seed.example:8123"


def test_ha_empty_yaml_string_still_overrides_env() -> None:
    """Even an explicit empty string from YAML is authoritative.
    Operators can clear a field via WebUI without env silently
    re-populating it."""
    with _env(HA_TOKEN="env-token"):
        ha = HomeAssistantGlobal.model_validate({"token": ""})
        assert ha.token == ""


def test_model_options_yaml_wins_over_env() -> None:
    """Same precedence for Ollama tuning knobs — WebUI Save persists
    regardless of OLLAMA_* env vars in the compose file."""
    from glados.core.config_store import ModelOptionsConfig
    with _env(OLLAMA_TEMPERATURE="0.1", OLLAMA_NUM_CTX="2048"):
        opts = ModelOptionsConfig.model_validate({
            "temperature": 0.95,
            "num_ctx": 32768,
        })
        assert opts.temperature == 0.95
        assert opts.num_ctx == 32768


def test_model_options_env_seeds_when_yaml_missing() -> None:
    from glados.core.config_store import ModelOptionsConfig
    with _env(OLLAMA_TEMPERATURE="0.35", OLLAMA_NUM_CTX="4096"):
        opts = ModelOptionsConfig.model_validate({})
        assert opts.temperature == 0.35
        assert opts.num_ctx == 4096


def test_model_options_invalid_env_falls_back_to_default() -> None:
    """Boot must not crash if someone sets OLLAMA_TEMPERATURE=abc."""
    from glados.core.config_store import _env_float, _env_int
    assert _env_float("DEFINITELY_UNSET_KEY", 0.5) == 0.5
    with _env(BAD_FLOAT="not-a-number"):
        assert _env_float("BAD_FLOAT", 0.7) == 0.7
    with _env(BAD_INT="xyz"):
        assert _env_int("BAD_INT", 99) == 99


def test_autonomy_vision_default_to_interactive_when_env_unset() -> None:
    # Phase 6 follow-up: Option C — operators can split autonomy/vision
    # onto a separate Ollama if they want, but the out-of-the-box default
    # unifies everything on OLLAMA_URL. Structural guard so the fallback
    # chain doesn't regress; env-evaluation is tested indirectly by the
    # "defaults_use_docker_service_names" case above (all three resolve
    # to the same http://ollama:11434 when no env vars are set).
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parent.parent / "glados" / "core" / "config_store.py").read_text(encoding="utf-8")
    # Autonomy must fall back to OLLAMA_URL (not a hardcoded separate default).
    assert '_env("OLLAMA_AUTONOMY_URL", _env("OLLAMA_URL"' in src, (
        "Autonomy URL must fall back to OLLAMA_URL, then the pydantic default"
    )
    assert '_env("OLLAMA_VISION_URL", _env("OLLAMA_URL"' in src, (
        "Vision URL must fall back to OLLAMA_URL, then the pydantic default"
    )


def test_services_yaml_url_still_wins_over_default() -> None:
    # Services do NOT use env-overrides-YAML (unlike HomeAssistantGlobal) —
    # this is intentional per the Phase 6 migration policy: operators with
    # legacy host-specific URLs in services.yaml keep working silently. A
    # later release may flip the precedence once YAML URLs are fully retired.
    s = ServicesConfig.model_validate({
        "llm_interactive": {"url": "http://10.0.0.10:11434"},
    })
    assert s.llm_interactive.url == "http://10.0.0.10:11434"
    # Untouched fields still resolve from pydantic defaults (self-contained).
    # TTS / STT / api_wrapper default to the loopback internal port
    # (see TLS-everywhere migration, 2026-04-29).
    assert s.tts.url == "http://127.0.0.1:18015"


# ── Backward compat: existing YAML still parses ────────────────────────


def test_existing_yaml_with_full_urls_still_parses() -> None:
    # Operators upgrading from pre-Phase-6 configs have hardcoded IPs
    # in services.yaml / global.yaml. Those must continue to work.
    legacy_services = ServicesConfig.model_validate({
        "llm_interactive": {"url": "http://10.0.0.10:11434"},
        "tts": {"url": "http://10.0.0.10:5050", "voice": "glados"},
    })
    assert legacy_services.llm_interactive.url == "http://10.0.0.10:11434"
    assert legacy_services.tts.url == "http://10.0.0.10:5050"

    legacy_global = GlobalConfig.model_validate({
        "home_assistant": {"url": "http://10.0.0.20:8123"},
    })
    assert legacy_global.home_assistant.url == "http://10.0.0.20:8123"


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
    # Phase 6.4 (2026-04-22): WeatherGlobal.temperature_unit and
    # wind_speed_unit are no longer deprecated — they're the canonical
    # operator-facing unit preferences now that the Integrations →
    # Weather tab consolidates configuration. See WeatherGlobal
    # docstring for the consolidation rationale.
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


# ── Partial-save regression (no-wipe contract) ─────────────────────────


def test_partial_global_save_preserves_auth_block(tmp_path) -> None:
    """Live incident 2026-04-23: saving the HA tab via WebUI wiped the
    auth.password_hash field because update_section rebuilt the whole
    GlobalConfig from defaults. This test locks the merge-on-write
    behaviour — partial posts must preserve untouched fields."""
    import yaml as _yaml

    from glados.core.config_store import GladosConfigStore

    # Seed a populated global.yaml on disk
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    (configs_dir / "global.yaml").write_text(_yaml.dump({
        "home_assistant": {
            "url": "http://original:8123",
            "ws_url": "ws://original:8123/api/websocket",
            "token": "original-token",
        },
        "auth": {
            "enabled": True,
            "password_hash": "$2b$12$fake.hash.preserved",
            "session_secret": "preserved-secret-xyz",
        },
        "audit": {"enabled": True},
    }), encoding="utf-8")

    store = GladosConfigStore()
    store.load(configs_dir=configs_dir)

    # WebUI posts only the home_assistant block (Integrations → HA tab)
    store.update_section("global", {
        "home_assistant": {
            "url": "http://updated:8123",
            "ws_url": "ws://updated:8123/api/websocket",
            "token": "rotated-token",
        },
    })

    # New HA values landed
    assert store.global_.home_assistant.token == "rotated-token"
    assert store.global_.home_assistant.url == "http://updated:8123"

    # Auth block MUST survive the partial save untouched.
    assert store.global_.auth.password_hash == "$2b$12$fake.hash.preserved"
    assert store.global_.auth.session_secret == "preserved-secret-xyz"
    assert store.global_.auth.enabled is True

    # And the YAML on disk reflects that — next load will still have it
    reloaded = _yaml.safe_load(
        (configs_dir / "global.yaml").read_text(encoding="utf-8")
    )
    assert reloaded["auth"]["password_hash"] == "$2b$12$fake.hash.preserved"
    assert reloaded["auth"]["session_secret"] == "preserved-secret-xyz"


def test_partial_global_save_preserves_session_secret_when_auth_block_missing_field(tmp_path) -> None:
    """Live incident 2026-04-27: operator's WebUI was rendered logged-out
    after a Save (no response, sidebar entries vanished, chat replied
    "Failed to fetch"). Container logs showed every authenticated
    request raising RuntimeError("auth.session_secret is empty").

    Root cause: ``_get_config_section`` masks ``auth.session_secret`` and
    ``home_assistant.token`` from GET responses (correctly — don't leak
    secrets to the browser). But on PUT, the shallow merge in
    update_section replaces the ``auth`` dict wholesale with whatever
    the WebUI sent, and the WebUI hadn't been given session_secret to
    send back. Result: the session-signing key got nuked, all session
    cookies became invalid, every authenticated endpoint 500'd, sidebar
    couldn't render past role-aware gating, and the operator was
    effectively locked out.

    Contract this test locks: when an incoming partial save omits a
    masked sensitive field (or sends it as empty/masked-form), the
    on-disk value MUST be preserved.
    """
    import yaml as _yaml

    from glados.core.config_store import GladosConfigStore

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    (configs_dir / "global.yaml").write_text(_yaml.dump({
        "home_assistant": {
            "url": "http://original:8123",
            "ws_url": "ws://original:8123/api/websocket",
            "token": "original-secret-token-not-leaked-to-ui",
        },
        "auth": {
            "enabled": True,
            "session_secret":
                "a824c35be8082e54e64bd196c6c2aab685bc937e1c527fd69b97ae9bab9e8bbf",
            "users": [],
        },
        "audit": {"enabled": True},
    }), encoding="utf-8")

    store = GladosConfigStore()
    store.load(configs_dir=configs_dir)

    # Simulate the WebUI POSTing the auth block back without
    # session_secret (because GET stripped it). Also send HA token in
    # the masked form ("first20...last8") that GET hands the browser.
    store.update_section("global", {
        "auth": {
            "enabled": False,            # operator toggled auth off
            "users": [],
        },
        "home_assistant": {
            "url": "http://original:8123",
            "ws_url": "ws://original:8123/api/websocket",
            "token": "original-secret-tok...not_leak",  # masked form
        },
    })

    # Operator's edit landed
    assert store.global_.auth.enabled is False

    # Sensitive fields preserved from on-disk values.
    assert store.global_.auth.session_secret == (
        "a824c35be8082e54e64bd196c6c2aab685bc937e1c527fd69b97ae9bab9e8bbf"
    ), "session_secret must survive a partial save that omits it"
    assert store.global_.home_assistant.token == \
        "original-secret-token-not-leaked-to-ui", \
        "HA token must survive a partial save that includes only the masked form"

    # And the YAML on disk reflects preservation.
    reloaded = _yaml.safe_load(
        (configs_dir / "global.yaml").read_text(encoding="utf-8")
    )
    assert reloaded["auth"]["session_secret"] != ""
    assert "..." not in reloaded["home_assistant"]["token"]


# ── Auth schema (added 2026-04-24 for multi-user rebuild) ──────────

def test_auth_global_new_shape_defaults():
    from glados.core.config_store import AuthGlobal
    a = AuthGlobal()
    assert a.enabled is True
    assert a.session_secret == ""
    assert a.session_timeout == "30d"
    assert a.session_idle_timeout == "0"
    assert a.bootstrap_allowed is True
    assert a.users == []
    assert a.rate_limits.login_max_attempts == 5
    assert a.rate_limits.service_max_requests == 10


def test_auth_global_legacy_fields_still_parse():
    from glados.core.config_store import AuthGlobal
    a = AuthGlobal.model_validate({
        "enabled": True,
        "password_hash": "$2b$12$legacyhash",
        "session_secret": "abc123",
        "session_timeout_hours": 24,
    })
    assert a.password_hash == "$2b$12$legacyhash"
    assert a.session_timeout_hours == 24
    assert a.users == []


def test_auth_user_config_defaults():
    from glados.core.config_store import UserConfig
    u = UserConfig(username="alice", password_hash="$argon2id$...")
    assert u.role == "chat"
    assert u.hash_algorithm == "argon2id"
    assert u.disabled is False
    assert u.display_name == "alice"


def test_auth_user_config_rejects_unknown_role():
    import pydantic
    from glados.core.config_store import UserConfig
    with pytest.raises(pydantic.ValidationError):
        UserConfig(username="x", password_hash="h", role="superuser")


def test_auth_global_deprecated_fields_emit_warnings() -> None:
    """Legacy YAML with password_hash / hash_algorithm / session_timeout_hours
    must emit a loguru WARNING — same pattern every other model with
    deprecated fields uses (NetworkGlobal, AuditGlobal, etc.)."""
    with _capture_warnings() as msgs:
        AuthGlobal.model_validate({
            "password_hash": "$2b$12$legacyhash",
            "hash_algorithm": "argon2id",
            "session_timeout_hours": 24,
        })
    joined = "\n".join(msgs)
    assert "password_hash" in joined, (
        f"Expected deprecation warning for password_hash; got: {joined!r}"
    )
    assert "session_timeout_hours" in joined, (
        f"Expected deprecation warning for session_timeout_hours; got: {joined!r}"
    )
    assert "deprecated" in joined.lower(), (
        f"Expected 'deprecated' in warning text; got: {joined!r}"
    )


def test_auth_global_no_warning_on_default_construction() -> None:
    """Instantiating AuthGlobal with no YAML must be silent — fresh
    installs shouldn't see deprecation noise for the legacy fields."""
    with _capture_warnings() as msgs:
        AuthGlobal()
    assert msgs == [], f"Expected no warnings on default construction, got: {msgs!r}"


# ── llm_* schema rename + llm_triage slot (2026-04-28) ─────────────────
# Schema-level rename from ``ollama_*`` to ``llm_*``. Pydantic
# ``AliasChoices`` keeps operators' existing services.yaml files (with
# ``ollama_*`` keys) parsing cleanly for one release; on save the file
# is rewritten with the new names. New ``llm_triage`` slot defaults to
# ``llama-3.2-1b-instruct`` so a fresh install routes triage subagents
# to a small fast model out of the box.


def test_config_loads_legacy_ollama_keys(tmp_path):
    """Operators with services.yaml from before the rename must keep working."""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "services.yaml").write_text(
        "ollama_interactive:\n"
        "  url: http://example:11434/v1/chat/completions\n"
        "  model: qwen3-30b-a3b\n"
        "ollama_autonomy:\n"
        "  url: http://example:11434/v1/chat/completions\n"
        "  model: qwen3-30b-a3b\n"
        "ollama_vision:\n"
        "  url: http://example:11434/v1/chat/completions\n"
        "  model: qwen2.5-vl-3b-instruct\n"
    )
    from glados.core.config_store import GladosConfigStore
    store = GladosConfigStore()
    store.load(configs_dir=cfgs)
    assert store.services.llm_interactive.model == "qwen3-30b-a3b"
    assert store.services.llm_autonomy.model == "qwen3-30b-a3b"
    assert store.services.llm_vision.model == "qwen2.5-vl-3b-instruct"


def test_config_loads_new_llm_keys(tmp_path):
    """New on-disk shape with llm_* keys parses identically."""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "services.yaml").write_text(
        "llm_interactive:\n"
        "  url: http://example:11434/v1/chat/completions\n"
        "  model: qwen3-30b-a3b\n"
        "llm_triage:\n"
        "  url: http://example:11434/v1/chat/completions\n"
        "  model: llama-3.2-1b-instruct\n"
    )
    from glados.core.config_store import GladosConfigStore
    store = GladosConfigStore()
    store.load(configs_dir=cfgs)
    assert store.services.llm_interactive.model == "qwen3-30b-a3b"
    assert store.services.llm_triage.model == "llama-3.2-1b-instruct"


def test_llm_triage_default():
    """Triage slot defaults to Llama-3.2-1B-Instruct so a fresh install
    routes triage subagents to the small fast model out of the box."""
    s = ServicesConfig()
    assert s.llm_triage.model == "llama-3.2-1b-instruct"


def test_config_save_writes_llm_keys():
    """Round-trip: a ServicesConfig parsed from legacy ollama_* keys must
    dump under the new llm_* names — operators' on-disk YAML gets
    rewritten to the new schema on the next save."""
    s = ServicesConfig.model_validate({
        "ollama_interactive": {"url": "http://x:11434", "model": "qwen3"},
        "ollama_autonomy": {"url": "http://x:11434", "model": "qwen3"},
        "ollama_vision": {"url": "http://x:11434", "model": "vl"},
    })
    dumped = s.model_dump(exclude_none=True)
    assert "llm_interactive" in dumped
    assert "llm_autonomy" in dumped
    assert "llm_vision" in dumped
    assert "llm_triage" in dumped
    assert "ollama_interactive" not in dumped
    assert "ollama_autonomy" not in dumped
    assert "ollama_vision" not in dumped
