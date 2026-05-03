"""Plugin triage — bypass-mode contract.

Triage runs INLINE on the chat path when the keyword pre-filter
returned no plugins. Bypass mode skips any LLM call and returns the
full enabled-plugin catalog so the chat LLM can advertise plugin
tools every turn (operator decision after the OVMS-on-Qwen3-30B
inline-triage latency stall — see CHANGES.md / handoff
2026-05-03-spotify-plugin-and-triage.md).

The contract this module guarantees:
* never raises
* returns ``[]`` when triage is disabled or inputs are degenerate
* otherwise returns ``[p.name for p in plugins]`` verbatim
* respects ``GLADOS_PLUGIN_TRIAGE_ENABLED``
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class _FakeManifest:
    description: str
    intent_keywords: list[str]


@dataclass
class _FakePlugin:
    name: str
    manifest_v2: Any


def _plugin(name: str, description: str = "") -> _FakePlugin:
    return _FakePlugin(
        name=name,
        manifest_v2=_FakeManifest(description=description, intent_keywords=[]),
    )


@pytest.fixture(autouse=True)
def _enable_triage(monkeypatch):
    """Default the env to enabled so individual tests opt INTO disabled."""
    monkeypatch.setenv("GLADOS_PLUGIN_TRIAGE_ENABLED", "true")


def test_returns_all_enabled_plugin_names():
    """Bypass mode advertises every plugin in the enabled catalog."""
    from glados.plugins.triage import triage_plugins
    plugins = [
        _plugin("arr-stack", "Manage Sonarr/Radarr movie + TV libraries"),
        _plugin("calendar", "Read and create calendar events"),
        _plugin("spotify", "Play music"),
    ]
    out = triage_plugins("totally unrelated message about the weather", plugins)
    assert out == ["arr-stack", "calendar", "spotify"]


def test_message_content_does_not_filter():
    """Bypass mode is content-agnostic — same plugins out for any non-
    empty message."""
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("arr-stack", "movies"), _plugin("calendar", "events")]
    a = triage_plugins("what's the forecast", plugins)
    b = triage_plugins("add a movie to my library", plugins)
    c = triage_plugins("hello there", plugins)
    assert a == b == c == ["arr-stack", "calendar"]


def test_preserves_plugin_order():
    """Order in == order out. Stable for downstream tool-catalog
    construction in the chat path."""
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("z-last"), _plugin("a-first"), _plugin("m-middle")]
    out = triage_plugins("any", plugins)
    assert out == ["z-last", "a-first", "m-middle"]


def test_env_disabled_short_circuits(monkeypatch):
    """GLADOS_PLUGIN_TRIAGE_ENABLED=false skips entirely — chat path
    sees no plugin tools regardless of what's enabled."""
    monkeypatch.setenv("GLADOS_PLUGIN_TRIAGE_ENABLED", "false")
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("arr-stack", "movies")]
    out = triage_plugins("what movies do I have", plugins)
    assert out == []


@pytest.mark.parametrize("falsy", ["false", "0", "no", "off", "FALSE", "Off"])
def test_env_falsy_variants_all_disable(monkeypatch, falsy):
    """Env truthy/falsy semantics match GLADOS_PLUGINS_ENABLED — case-
    insensitive, common falsy strings recognised."""
    monkeypatch.setenv("GLADOS_PLUGIN_TRIAGE_ENABLED", falsy)
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("arr-stack", "movies")]
    assert triage_plugins("any", plugins) == []


def test_empty_plugin_list_short_circuits():
    from glados.plugins.triage import triage_plugins
    assert triage_plugins("anything", []) == []


def test_empty_message_short_circuits():
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("arr-stack", "movies")]
    assert triage_plugins("   ", plugins) == []


def test_empty_string_message_short_circuits():
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("arr-stack", "movies")]
    assert triage_plugins("", plugins) == []


def test_timeout_s_accepted_but_ignored():
    """Back-compat: callers still pass ``timeout_s``; bypass mode
    accepts it but does nothing with it."""
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("arr-stack", "movies")]
    out = triage_plugins("any", plugins, timeout_s=0.001)
    assert out == ["arr-stack"]
