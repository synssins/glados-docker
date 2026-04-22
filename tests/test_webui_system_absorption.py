"""Stage 3 Phase 6 follow-up — System tab absorbs auth, audit, and the
two maintenance_* mode-entity fields.

Structural assertions on tts_ui.py. Behavior was verified interactively
via the Claude Preview MCP (forms render, Integrations drops the groups,
payload round-trips through pydantic).
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


# ── Integrations no longer renders the absorbed groups ─────────────────


def test_integrations_skipkeys_extended(source: str) -> None:
    # The Integrations branch must skip auth, audit, and mode_entities
    # in addition to the Phase 6 Commit 1 list.
    m = re.search(
        r"backing\s*===\s*'global'\)?\s*\?\s*(\[[^\]]*\])",
        source,
    )
    assert m, "Could not find global-backing skipKeys array"
    array_src = m.group(1)
    for key in ("'ssl'", "'paths'", "'network'", "'auth'", "'audit'", "'mode_entities'"):
        assert key in array_src, f"skipKeys must include {key}"


# ── System tab new cards ───────────────────────────────────────────────


def test_system_has_maintenance_entities_card(source: str) -> None:
    # Card header + form div + save button + result span.
    assert re.search(
        r'<div class="section-title">\s*Maintenance Entities\s*</div>',
        source,
    ), "Expected 'Maintenance Entities' card header on System"
    assert 'id="sysMaintForm"' in source
    assert "cfgSaveSystemMaint" in source
    assert 'id="cfg-save-result-sys-maint"' in source


def test_system_has_authentication_and_audit_card(source: str) -> None:
    # HTML entity for & is written as &amp; in the JS string literal.
    assert '<div class="section-title">Authentication &amp; Audit</div>' in source, (
        "Expected 'Authentication & Audit' card header on System"
    )
    assert 'id="sysAuthAuditForm"' in source
    assert "cfgSaveSystemAuthAudit" in source
    assert 'id="cfg-save-result-sys-authaudit"' in source


# ── Render + save helpers ──────────────────────────────────────────────


def test_system_config_cards_loader_wired_into_nav(source: str) -> None:
    # navigateTo's config.system branch must call loadSystemConfigCards
    # so the forms populate when the tab activates.
    assert re.search(
        r"loadSystemConfigCards\(\)",
        source,
    ), "Expected loadSystemConfigCards() to be called from navigateTo"


def test_render_functions_use_sysaux_section_to_avoid_id_collisions(source: str) -> None:
    # Form IDs under System use the 'sysaux' prefix so they don't clash
    # with Integrations' cfg-global-* inputs (both panels can live in the
    # DOM simultaneously because inactive tabs retain their content).
    assert "cfgBuildForm(subset, 'sysaux'" in source, (
        "Expected System-tab form renders to use section='sysaux'"
    )


def _extract_function_body(source: str, name: str) -> str:
    """Return the body of a JS function by brace-matching from the
    opening `function name(` all the way to the matching close."""
    start = source.find(f"function {name}(")
    assert start >= 0, f"function {name} not found"
    # Walk from the first `{` after the signature
    i = source.find("{", start)
    depth = 0
    while i < len(source):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces in function {name}")


def test_render_maint_form_only_exposes_maintenance_fields(source: str) -> None:
    body = _extract_function_body(source, "_cfgRenderSystemMaintForm")
    # Strip JS single-line comments so the test isn't tripped by
    # "silent_mode / dnd belong on Audio & Speakers" explanatory text.
    code = re.sub(r"//[^\n]*", "", body)
    # The two maintenance_* fields must be assigned into the subset dict.
    assert re.search(r"\bmaintenance_mode\s*:", code), (
        "Subset dict must include maintenance_mode"
    )
    assert re.search(r"\bmaintenance_speaker\s*:", code), (
        "Subset dict must include maintenance_speaker"
    )
    # Silent_mode and dnd must NOT be assigned into the subset — they
    # belong on Audio & Speakers, not System.
    assert not re.search(r"\bsilent_mode\s*:", code), (
        "silent_mode must NOT render on the Maintenance Entities card"
    )
    assert not re.search(r"\bdnd\s*:", code), (
        "dnd must NOT render on the Maintenance Entities card"
    )


def test_render_auth_audit_includes_both_groups(source: str) -> None:
    body = _extract_function_body(source, "_cfgRenderSystemAuthAuditForm")
    assert "g.auth" in body and "g.audit" in body


def test_save_routes_to_global_endpoint(source: str) -> None:
    # Both saves (maint + auth/audit) go to /api/config/global via the
    # shared _cfgSaveSystemSubset helper.
    assert "_cfgSaveSystemSubset" in source
    assert re.search(
        r"fetch\(\s*['\"]/api/config/global['\"]",
        source,
    ), "Expected PUT /api/config/global from the system save path"


def test_save_deep_merges_into_existing_global(source: str) -> None:
    # The helper must merge the collected delta into a snapshot of
    # _cfgData.global so sibling fields (home_assistant, silent_hours,
    # weather, etc.) are preserved on save.
    assert re.search(
        r"JSON\.parse\(JSON\.stringify\(_cfgData\.global",
        source,
    ), "Save helper must snapshot current _cfgData.global before merging delta"
    assert "_merge(next, delta)" in source


def test_save_handles_plain_text_error_response(source: str) -> None:
    # Legacy /api/config/<section> uses _send_error for failures, which
    # emits plain text. Defensive JSON.parse so we don't surface
    # "Unexpected token 'V'" instead of the real error.
    assert re.search(
        r"try\s*\{\s*resp\s*=\s*JSON\.parse\(bodyText\)\s*;?\s*\}\s*catch\b",
        source,
    ), "Save helper must gracefully handle non-JSON error bodies"


# ── FIELD_META tweaks — the absorbed fields become visible-by-default ──


@pytest.mark.parametrize("field_path", [
    "auth.enabled",
    "auth.session_timeout_hours",
    "audit.enabled",
    "mode_entities.maintenance_mode",
    "mode_entities.maintenance_speaker",
])
def test_absorbed_field_is_no_longer_advanced(source: str, field_path: str) -> None:
    # Match the specific META entry and assert it does NOT carry
    # advanced: true. Otherwise these fields would be hidden by default
    # on the System tab (which has no Advanced toggle of its own).
    pattern = re.compile(
        r"'" + re.escape(field_path) + r"'\s*:\s*\{([^}]*)\}",
    )
    m = pattern.search(source)
    assert m, f"Missing FIELD_META entry for {field_path!r}"
    body = m.group(1)
    assert "advanced" not in body or "advanced: false" in body, (
        f"FIELD_META[{field_path!r}] must not be advanced: true — it now "
        "renders on System by default"
    )


# ── Sensitive auth fields stay advanced ────────────────────────────────


@pytest.mark.parametrize("field_path", [
    "auth.password_hash",
    "auth.session_secret",
])
def test_sensitive_auth_field_remains_advanced(source: str, field_path: str) -> None:
    pattern = re.compile(
        r"'" + re.escape(field_path) + r"'\s*:\s*\{[^}]*advanced\s*:\s*true",
    )
    assert pattern.search(source), (
        f"{field_path!r} must remain advanced — operators don't edit "
        "these through the form (password hash is set via a CLI tool)"
    )
