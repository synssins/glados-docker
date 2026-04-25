"""Stage 3 Phase 6 — Commit 5 coverage: user-friendly defaults.

Structural tests on tts_ui.py. Each test locks in a specific expectation
so future PRs can't silently surface a field we intentionally hid or
remove a placeholder we promised to show.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TTS_UI = Path(__file__).resolve().parent.parent / "glados" / "webui" / "tts_ui.py"


@pytest.fixture(scope="module")
def source() -> str:
    from tests._webui_source import webui_combined_source
    return webui_combined_source()


# ── `hidden: true` mechanism ───────────────────────────────────────────


def test_cfg_build_form_skips_hidden_fields(source: str) -> None:
    # The guard must fire BEFORE any HTML is emitted for the field, and
    # must key off the FIELD_META lookup for that path.
    assert re.search(
        r"if\s*\(\s*meta\.hidden\s*\)\s*continue",
        source,
    ), "cfgBuildForm must skip fields whose FIELD_META entry has hidden: true"


def test_group_skipped_when_all_children_hidden(source: str) -> None:
    assert "visibleChildKeys.length === 0" in source, (
        "cfgBuildForm must skip whole groups when every child is hidden, "
        "otherwise an empty <div class='cfg-group'> heading renders"
    )


def test_group_advanced_check_uses_only_visible_children(source: str) -> None:
    # The groupAdvanced check must use visibleChildKeys (not all childKeys)
    # so a group with hidden children + advanced-visible children still
    # collapses behind the Advanced toggle.
    assert re.search(
        r"const\s+groupAdvanced\s*=\s*visibleChildKeys\.every",
        source,
    ), "groupAdvanced must iterate visibleChildKeys, not the raw child list"


# ── FIELD_META hidden entries ──────────────────────────────────────────


@pytest.mark.parametrize("field_path", [
    "paths.glados_root", "paths.audio_base", "paths.logs", "paths.data", "paths.assets",
    "network.serve_host", "network.serve_port",
    "audit.path", "audit.retention_days",
    "weather.temperature_unit", "weather.wind_speed_unit",
    "tuning.engine_audio_default",
    "ha_output_dir", "archive_dir", "tts_ui_output_dir",
    "chat_audio_dir", "announcements_dir", "commands_dir",
])
def test_deprecated_or_env_only_field_is_hidden(source: str, field_path: str) -> None:
    # Match the specific FIELD_META entry for this path and assert it
    # includes hidden: true. Regex tolerates order of attributes inside
    # the `{...}` object.
    pattern = re.compile(
        r"'"
        + re.escape(field_path)
        + r"'\s*:\s*\{[^}]*hidden\s*:\s*true",
        re.DOTALL,
    )
    assert pattern.search(source), (
        f"FIELD_META['{field_path}'] must be marked hidden: true"
    )


def test_services_gladys_api_hidden_from_services_grid(source: str) -> None:
    # cfgRenderServices iterates data; SERVICES_HIDDEN drops deprecated
    # endpoints (gladys_api) from the card list.
    assert re.search(
        r"SERVICES_HIDDEN\s*=\s*new\s+Set\(\[\s*'gladys_api'\s*\]\)",
        source,
    ), "gladys_api must be in SERVICES_HIDDEN so it disappears from the grid"
    assert "if (SERVICES_HIDDEN.has(key)) continue;" in source


# ── Integrations: MQTT + Media Stack placeholder cards ─────────────────


def test_integrations_has_mqtt_config_pane(source: str) -> None:
    # Phase 5.8 (2026-04-22): the old 'Coming soon' MQTT placeholder
    # was replaced by a real config pane. Operator can set broker
    # host / port / TLS / auth / client id / topic prefix via the UI
    # and the form PUTs to /api/config/mqtt. No broker coordinates
    # are hardcoded anywhere.
    assert "_cfgRenderIntegrationsExtras" in source
    assert "_cfgLoadMqtt" in source, (
        "Integrations must wire the MQTT config-pane loader"
    )
    assert "_cfgSaveMqtt" in source, (
        "MQTT config pane must expose a save function that PUTs the form"
    )
    assert 'id="cfg-mqtt-body"' in source, (
        "MQTT card must expose the #cfg-mqtt-body mount point"
    )
    assert "/api/config/mqtt" in source, (
        "MQTT save handler must POST to /api/config/mqtt"
    )
    # The 'Coming soon' placeholder must be gone.
    assert not re.search(
        r"cfg-placeholder-title[^>]*>\s*MQTT",
        source,
    ), "MQTT placeholder should have been replaced by a real config pane"


def test_integrations_no_longer_has_media_stack_placeholder(source: str) -> None:
    # Phase 6.0 (2026-04-22): Integrations was restructured into top-
    # tabs (HA / MQTT / Disambiguation / Candidate retrieval). The
    # 'Media Stack — Coming soon' placeholder was dropped in the
    # same pass; when that work is actually scoped it becomes a
    # proper tab, not a placeholder card. Locks the removal in.
    assert not re.search(
        r"cfg-placeholder-title[^>]*>\s*Media Stack",
        source,
    ), "Media Stack placeholder should have been removed in Phase 6.0"


# ── LLM & Services: Model Options + LLM Timeouts (now in System → Services) ───


def test_llm_services_has_model_options_card(source: str) -> None:
    # Phase 2 Chunk 2: Model Options moved into loadSystemServices() as an
    # Advanced collapsible on the System → Services tab. The old
    # _cfgRenderLLMServicesExtras function is gone; loadSystemServices renders
    # the same fields inline.
    assert "loadSystemServices" in source, "loadSystemServices must exist"
    assert re.search(
        r"cfg-subsection-title[^>]*>\s*Model Options",
        source,
    ), "System Services tab must render a Model Options subsection"
    # Four fields: temperature, top_p, num_ctx, repeat_penalty
    for field in ("temperature", "top_p", "num_ctx", "repeat_penalty"):
        assert f"model_options.{field}" in source, (
            f"Model Options section missing input for {field!r}"
        )


def test_llm_services_has_llm_timeouts_card_marked_advanced(source: str) -> None:
    # Find the LLM Timeouts card opening and assert data-advanced is present.
    # Attribute order can vary (class/style/data-advanced), so we just
    # require both markers to be in the same opening <div>.
    # Phase 6.2 (2026-04-22): relaxed the inter-statement whitespace
    # pattern — the JS source was re-indented when LLM extras moved
    # to _cfgRenderLLMExtrasOnly. Multiple spaces between 'html +='
    # and the opening quote are now valid.
    m = re.search(
        r"<div([^>]*)>'\s*;\s*html\s*\+=\s*'<div class=\"cfg-subsection-title\">LLM Timeouts",
        source,
    )
    assert m, "Expected a <div> opening immediately before the LLM Timeouts subsection title"
    attrs = m.group(1)
    assert 'class="card"' in attrs, "LLM Timeouts wrapper must be a .card"
    assert 'data-advanced="true"' in attrs, (
        "LLM Timeouts card must be marked advanced (operators rarely touch)"
    )


def test_llm_services_model_options_save_uses_personality_endpoint(source: str) -> None:
    assert re.search(
        r"cfgSaveModelOptions.*?/api/config/personality",
        source, re.DOTALL,
    ), "Model Options save must PUT /api/config/personality (backing store)"


def test_llm_services_timeouts_save_uses_global_endpoint(source: str) -> None:
    assert re.search(
        r"cfgSaveLLMTimeouts.*?/api/config/global",
        source, re.DOTALL,
    ), "LLM Timeouts save must PUT /api/config/global (backing store)"
