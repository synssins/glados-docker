"""LLM-backed plugin intent triage (Phase 2c gate #2).

The triage call runs INLINE on the chitchat path with a 1.5 s budget,
so the contract is: never raise, always return a clean list of names
that exist in the enabled plugin set, and respect the
GLADOS_PLUGIN_TRIAGE_ENABLED env switch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

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


def test_happy_path_returns_relevant_names():
    from glados.plugins.triage import triage_plugins
    plugins = [
        _plugin("arr-stack", "Manage Sonarr/Radarr movie + TV libraries"),
        _plugin("calendar", "Read and create calendar events"),
    ]
    fake_response = '{"relevant": ["arr-stack"]}'
    with patch("glados.plugins.triage.llm_call", return_value=fake_response):
        out = triage_plugins("what movies do I have", plugins)
    assert out == ["arr-stack"]


def test_malformed_json_returns_empty():
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("arr-stack", "movies")]
    with patch("glados.plugins.triage.llm_call", return_value="not even close to json"):
        out = triage_plugins("anything", plugins)
    assert out == []


def test_filters_names_not_in_enabled_set():
    """LLM occasionally hallucinates plausible names; we MUST drop them
    so the chat path doesn't try to advertise tools from a non-existent
    plugin server."""
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("arr-stack", "movies")]
    fake_response = '{"relevant": ["arr-stack", "ghost-plugin", "another-fake"]}'
    with patch("glados.plugins.triage.llm_call", return_value=fake_response):
        out = triage_plugins("anything", plugins)
    assert out == ["arr-stack"]


def test_llm_call_returns_none_returns_empty():
    """llm_call returns None on timeout / connection error / empty body."""
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("arr-stack", "movies")]
    with patch("glados.plugins.triage.llm_call", return_value=None):
        out = triage_plugins("anything", plugins)
    assert out == []


def test_llm_call_raising_returns_empty():
    """Any exception from the LLM stack is swallowed -- the chitchat
    path falls back to no-tools rather than 500ing the request."""
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("arr-stack", "movies")]
    with patch("glados.plugins.triage.llm_call", side_effect=RuntimeError("boom")):
        out = triage_plugins("anything", plugins)
    assert out == []


def test_env_disabled_short_circuits(monkeypatch):
    """GLADOS_PLUGIN_TRIAGE_ENABLED=false skips the LLM entirely --
    no call should be made and the result is always []."""
    monkeypatch.setenv("GLADOS_PLUGIN_TRIAGE_ENABLED", "false")
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("arr-stack", "movies")]
    with patch("glados.plugins.triage.llm_call") as call:
        out = triage_plugins("what movies do I have", plugins)
    assert out == []
    call.assert_not_called()


def test_empty_plugin_list_short_circuits():
    from glados.plugins.triage import triage_plugins
    with patch("glados.plugins.triage.llm_call") as call:
        out = triage_plugins("anything", [])
    assert out == []
    call.assert_not_called()


def test_empty_message_short_circuits():
    from glados.plugins.triage import triage_plugins
    plugins = [_plugin("arr-stack", "movies")]
    with patch("glados.plugins.triage.llm_call") as call:
        out = triage_plugins("   ", plugins)
    assert out == []
    call.assert_not_called()
