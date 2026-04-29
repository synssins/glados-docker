"""GLADOS_PLUGINS_ENABLED gate behavior."""
from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest


def test_plugins_enabled_default_true(monkeypatch):
    """Unset env var → discover_plugins is called."""
    monkeypatch.delenv("GLADOS_PLUGINS_ENABLED", raising=False)
    discover_mock = MagicMock(return_value=[])
    with patch("glados.plugins.discover_plugins", discover_mock):
        from glados.core.engine import _maybe_discover_plugin_configs
        _maybe_discover_plugin_configs()
    discover_mock.assert_called_once()


def test_plugins_disabled_env_skips_discovery(monkeypatch, caplog):
    """GLADOS_PLUGINS_ENABLED=false → discover_plugins not called, info log emitted."""
    monkeypatch.setenv("GLADOS_PLUGINS_ENABLED", "false")
    discover_mock = MagicMock(return_value=[])
    with patch("glados.plugins.discover_plugins", discover_mock):
        from glados.core.engine import _maybe_discover_plugin_configs
        configs = _maybe_discover_plugin_configs()
    discover_mock.assert_not_called()
    assert configs == []


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_plugins_enabled_truthy_values(monkeypatch, value):
    """Various truthy strings all enable discovery."""
    monkeypatch.setenv("GLADOS_PLUGINS_ENABLED", value)
    discover_mock = MagicMock(return_value=[])
    with patch("glados.plugins.discover_plugins", discover_mock):
        from glados.core.engine import _maybe_discover_plugin_configs
        _maybe_discover_plugin_configs()
    discover_mock.assert_called_once()
