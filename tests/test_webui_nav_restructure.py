"""Stage 3 Phase 6 — Commit 4 coverage: Configuration sidebar restructure.

Structural tests on the JS/HTML in tts_ui.py. Behavior was verified
interactively via the Claude Preview MCP (dev harness on port 28052).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TTS_UI = Path(__file__).resolve().parent.parent / "glados" / "webui" / "tts_ui.py"


@pytest.fixture(scope="module")
def source() -> str:
    return TTS_UI.read_text(encoding="utf-8")


# ── Sidebar nav entries ────────────────────────────────────────────────


@pytest.mark.parametrize("nav_key, label", [
    ("config.system", "System"),
    ("config.integrations", "Integrations"),
    ("config.llm-services", "LLM &amp; Services"),
    ("config.audio-speakers", "Audio &amp; Speakers"),
    ("config.personality", "Personality"),
    ("config.memory", "Memory"),
    ("config.ssl", "SSL"),
    ("config.raw", "Raw YAML"),
])
def test_sidebar_contains_phase6_nav_entry(source: str, nav_key: str, label: str) -> None:
    pattern = re.compile(
        r'data-nav-key="' + re.escape(nav_key) + r'"[^>]*>' + re.escape(label) + r'</a>',
    )
    assert pattern.search(source), f"Missing sidebar nav entry for {nav_key!r} ({label!r})"


@pytest.mark.parametrize("removed_key", [
    "config.global",
    "config.services",
    "config.speakers",
    "config.audio",
])
def test_removed_nav_entries_no_longer_rendered_in_sidebar(source: str, removed_key: str) -> None:
    # Narrow the search to the sidebar <nav-children> block; topbar shortcut
    # may still reference config.integrations/etc.
    m = re.search(r'<div class="nav-children">(.*?)</div>', source, re.DOTALL)
    assert m, "Missing nav-children block"
    sidebar_html = m.group(1)
    pattern = 'data-nav-key="' + removed_key + '"'
    assert pattern not in sidebar_html, (
        f"Legacy nav entry {removed_key!r} should not render in the sidebar anymore; "
        "operators reach its replacement via the Phase 6 nav items."
    )


# ── Legacy localStorage key migration ──────────────────────────────────


@pytest.mark.parametrize("legacy, target", [
    ("control", "config.system"),
    ("config", "config.integrations"),
    ("config.global", "config.integrations"),
    ("config.services", "config.llm-services"),
    ("config.speakers", "config.audio-speakers"),
    ("config.audio", "config.audio-speakers"),
])
def test_legacy_key_migrates_to_phase6_equivalent(source: str, legacy: str, target: str) -> None:
    pattern = re.compile(
        r"if\s*\(\s*k\s*===\s*'" + re.escape(legacy) + r"'\s*\)\s*return\s*'" + re.escape(target) + r"'",
    )
    assert pattern.search(source), (
        f"Expected legacy migration {legacy!r} -> {target!r} in _migrateLegacyKey"
    )


# ── Virtual-backing dispatch ───────────────────────────────────────────


def test_virtual_backing_map_routes_integrations_to_global(source: str) -> None:
    assert re.search(
        r"_CFG_BACKING\s*=\s*\{[^}]*'integrations'\s*:\s*'global'",
        source, re.DOTALL,
    ), "Integrations virtual page must route to the 'global' backing section"


def test_virtual_backing_map_routes_llm_services_to_services(source: str) -> None:
    assert re.search(
        r"_CFG_BACKING\s*=\s*\{[^}]*'llm-services'\s*:\s*'services'",
        source, re.DOTALL,
    ), "LLM & Services virtual page must route to the 'services' backing section"


def test_audio_speakers_has_custom_renderer(source: str) -> None:
    # Custom renderer is required because the page spans two backing
    # sections (speakers + audio) with their own per-subsection Save.
    assert "_cfgRenderAudioSpeakers" in source, \
        "Expected _cfgRenderAudioSpeakers helper"
    assert re.search(
        r"if\s*\(\s*section\s*===\s*'audio-speakers'\s*\)\s*\{\s*_cfgRenderAudioSpeakers\(\)",
        source,
    ), "cfgRenderSection must dispatch audio-speakers to _cfgRenderAudioSpeakers"


def test_audio_speakers_renders_two_save_buttons_for_backing_sections(source: str) -> None:
    # Quotes are escaped inside Python-string-wrapped JS: cfgSaveSection(\'speakers\', ...)
    assert re.search(
        r"cfgSaveSection\(\\?'speakers\\?',\s*\\?'cfg-save-result-speakers\\?'\)",
        source,
    ), "Audio & Speakers page must save the Speakers form to the 'speakers' backing"
    assert re.search(
        r"cfgSaveSection\(\\?'audio\\?',\s*\\?'cfg-save-result-audio\\?'\)",
        source,
    ), "Audio & Speakers page must save the Audio form to the 'audio' backing"


# ── cfgSaveSection accepts an optional result-element id ───────────────


def test_cfg_save_section_accepts_result_el_id(source: str) -> None:
    assert re.search(
        r"async\s+function\s+cfgSaveSection\s*\(\s*section\s*,\s*resultElId\s*\)",
        source,
    ), "cfgSaveSection must accept an optional resultElId for per-subsection saves"
