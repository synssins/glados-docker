"""Per-plugin `tool_guidance` field — schema + chat-path injection.

The upstream tool descriptions an MCP server provides aren't always
disambiguated enough for the model to pick the right one (operator-flagged
2026-05-03: model picked `mcp.*arr Stack.search` for "movies in my library"
when it should have picked `radarr_get_movies` — the upstream `search`
tool description says it covers TRaSH Guides too, so results aren't
guaranteed to be in the user's library).

Solution: each plugin can supply a short `tool_guidance` string in its
plugin.json. The chat path injects it as a system message ONLY when that
plugin is the active route. Operators without the plugin pay zero token
cost — the field doesn't exist, the injection doesn't fire.
"""

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


# ---------------------------------------------------------------------------
# Schema: PluginJSON.tool_guidance
# ---------------------------------------------------------------------------

def test_tool_guidance_defaults_to_none():
    from glados.plugins.bundle import PluginJSON
    p = PluginJSON.model_validate(_minimal())
    assert p.tool_guidance is None


def test_tool_guidance_accepts_short_string():
    from glados.plugins.bundle import PluginJSON
    guidance = "Use radarr_get_movies for library queries; search returns ADD-candidates."
    p = PluginJSON.model_validate(_minimal(tool_guidance=guidance))
    assert p.tool_guidance == guidance


def test_tool_guidance_rejects_over_600_chars():
    from glados.plugins.bundle import PluginJSON
    too_long = "x" * 601
    with pytest.raises(ValidationError, match="600|max_length|too long|at most"):
        PluginJSON.model_validate(_minimal(tool_guidance=too_long))


def test_tool_guidance_accepts_exactly_600_chars():
    from glados.plugins.bundle import PluginJSON
    boundary = "x" * 600
    p = PluginJSON.model_validate(_minimal(tool_guidance=boundary))
    assert len(p.tool_guidance or "") == 600


# ---------------------------------------------------------------------------
# Helper: build_plugin_guidance_message
# ---------------------------------------------------------------------------

class _StubManifest:
    def __init__(self, tool_guidance: str | None = None):
        self.tool_guidance = tool_guidance


class _StubPlugin:
    def __init__(self, name: str, tool_guidance: str | None = None):
        self.name = name
        self.manifest_v2 = _StubManifest(tool_guidance)


def test_helper_returns_none_when_no_matched_plugins():
    from glados.core.api_wrapper import build_plugin_guidance_message
    assert build_plugin_guidance_message([]) is None


def test_helper_returns_none_when_no_plugin_has_guidance():
    from glados.core.api_wrapper import build_plugin_guidance_message
    msg = build_plugin_guidance_message([
        _StubPlugin("A", tool_guidance=None),
        _StubPlugin("B", tool_guidance=None),
    ])
    assert msg is None


def test_helper_returns_system_message_with_single_plugin_guidance():
    from glados.core.api_wrapper import build_plugin_guidance_message
    msg = build_plugin_guidance_message([
        _StubPlugin("*arr Stack", tool_guidance="Use *_get_* for the library."),
    ])
    assert msg is not None
    assert msg["role"] == "system"
    assert "Tool selection guidance" in msg["content"]
    assert "[*arr Stack]" in msg["content"]
    assert "Use *_get_* for the library." in msg["content"]


def test_helper_combines_multiple_plugins_with_separators():
    from glados.core.api_wrapper import build_plugin_guidance_message
    msg = build_plugin_guidance_message([
        _StubPlugin("Alpha", tool_guidance="Alpha rule."),
        _StubPlugin("Beta", tool_guidance="Beta rule."),
    ])
    assert msg is not None
    assert "[Alpha]\nAlpha rule." in msg["content"]
    assert "[Beta]\nBeta rule." in msg["content"]
    # Double-newline between blocks for readability
    assert "Alpha rule.\n\n[Beta]" in msg["content"]


def test_helper_skips_plugins_without_guidance_when_some_have_it():
    from glados.core.api_wrapper import build_plugin_guidance_message
    msg = build_plugin_guidance_message([
        _StubPlugin("HasGuidance", tool_guidance="Use foo_get_bar."),
        _StubPlugin("NoGuidance", tool_guidance=None),
    ])
    assert msg is not None
    assert "[HasGuidance]" in msg["content"]
    assert "[NoGuidance]" not in msg["content"]


def test_helper_strips_whitespace_in_guidance():
    """Leading/trailing whitespace in the guidance string shouldn't
    bloat the prompt."""
    from glados.core.api_wrapper import build_plugin_guidance_message
    msg = build_plugin_guidance_message([
        _StubPlugin("X", tool_guidance="\n\n  Use x_get_y.  \n\n"),
    ])
    assert msg is not None
    assert "[X]\nUse x_get_y." in msg["content"]
    # Trailing whitespace from the input should not survive
    assert "x_get_y.  " not in msg["content"]


def test_helper_handles_plugin_without_manifest_v2_attribute():
    """Defensive: if a plugin object lacks `manifest_v2` (legacy or test
    stub), treat it as having no guidance rather than crashing."""
    from glados.core.api_wrapper import build_plugin_guidance_message

    class _Bare:
        name = "Bare"

    msg = build_plugin_guidance_message([_Bare()])
    assert msg is None
