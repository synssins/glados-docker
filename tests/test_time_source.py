"""Tests for ``glados.core.time_source``.

NTP and zoneinfo are mocked so the suite never touches the network or
depends on the host's tzdata being current.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pytest

from glados.core import time_source
from glados.core.config_store import TimeGlobal


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    time_source.reset_for_tests()
    yield
    time_source.reset_for_tests()


def _cfg(**overrides) -> TimeGlobal:
    return TimeGlobal(**overrides)


# ── tz resolution ─────────────────────────────────────────────────


def test_resolve_tz_falls_back_to_utc_when_unconfigured() -> None:
    """No configure() call → UTC. Defensive default; engine init order
    bugs shouldn't crash the chat path."""
    assert time_source._resolve_tz_name() == "UTC"


def test_resolve_tz_uses_manual_when_source_is_manual() -> None:
    time_source.configure(_cfg(timezone_source="manual", timezone_manual="America/Chicago"))
    assert time_source._resolve_tz_name() == "America/Chicago"


def test_resolve_tz_falls_back_to_utc_when_manual_empty() -> None:
    """timezone_source=manual with empty timezone_manual is misconfig
    — fall through to weather/UTC, don't crash with ZoneInfo('')."""
    time_source.configure(_cfg(timezone_source="manual", timezone_manual=""))
    assert time_source._resolve_tz_name() == "UTC"


def test_resolve_tz_reads_from_weather_cache_when_auto() -> None:
    getter = lambda: {"timezone": "America/Los_Angeles", "current": {}}
    time_source.configure(_cfg(timezone_source="auto"), weather_cache_getter=getter)
    assert time_source._resolve_tz_name() == "America/Los_Angeles"


def test_resolve_tz_handles_weather_cache_getter_exception() -> None:
    def boom():
        raise RuntimeError("cache unavailable")
    time_source.configure(_cfg(timezone_source="auto"), weather_cache_getter=boom)
    assert time_source._resolve_tz_name() == "UTC"


def test_resolve_tz_handles_missing_timezone_field_in_cache() -> None:
    """Old cache files predate the timezone capture — getter returns
    a dict but ``timezone`` key is absent. Don't crash, fall to UTC."""
    getter = lambda: {"current": {"temperature": 70}}
    time_source.configure(_cfg(timezone_source="auto"), weather_cache_getter=getter)
    assert time_source._resolve_tz_name() == "UTC"


def test_manual_tz_overrides_weather_cache() -> None:
    """Manual override wins even when the weather cache has a tz."""
    getter = lambda: {"timezone": "America/New_York"}
    time_source.configure(
        _cfg(timezone_source="manual", timezone_manual="Europe/London"),
        weather_cache_getter=getter,
    )
    assert time_source._resolve_tz_name() == "Europe/London"


# ── now() ──────────────────────────────────────────────────────────


def test_now_returns_tz_aware_datetime() -> None:
    time_source.configure(_cfg(timezone_source="manual", timezone_manual="America/Chicago"))
    dt = time_source.now()
    assert dt.tzinfo is not None
    assert str(dt.tzinfo) == "America/Chicago"


def test_now_applies_ntp_offset_after_sync() -> None:
    """When NTP sync stamps an offset onto _state, ``now()`` adds it
    to system ``time.time()``."""
    time_source.configure(_cfg(timezone_source="manual", timezone_manual="UTC"))

    fake_resp = SimpleNamespace(offset=120.0)  # +2 minutes
    fake_client = mock.MagicMock()
    fake_client.request.return_value = fake_resp
    with mock.patch("ntplib.NTPClient", return_value=fake_client):
        assert time_source._refresh_offset() is True

    # System time at fake epoch 1_000_000.0 + 120s offset = 1_000_120
    # → 1970-01-12 13:48:40 UTC.
    with mock.patch.object(time_source, "_time") as fake_time:
        fake_time.time.return_value = 1_000_000.0
        dt = time_source.now()
    expected = datetime(1970, 1, 12, 13, 48, 40, tzinfo=time_source.ZoneInfo("UTC"))
    assert dt == expected


# ── as_prompt() ───────────────────────────────────────────────────


def test_as_prompt_format_matches_spec() -> None:
    time_source.configure(_cfg(timezone_source="manual", timezone_manual="UTC"))
    fixed = datetime(2026, 5, 2, 13, 3, 0)
    with mock.patch.object(time_source, "now", return_value=fixed.replace(
        tzinfo=time_source.ZoneInfo("UTC"))):
        prompt = time_source.as_prompt()
    assert prompt == "Current time: Saturday 2026-05-02 13:03"


# ── _refresh_offset() — server fallback ──────────────────────────


def test_refresh_offset_falls_back_to_next_server() -> None:
    """First server raises (timeout, DNS fail, etc.); second responds.
    State reflects the second server's offset."""
    time_source.configure(_cfg(ntp_servers=["dead.example", "live.example"]))

    fake_client = mock.MagicMock()
    def request_side(server, **kwargs):
        if server == "dead.example":
            raise OSError("connection timed out")
        return SimpleNamespace(offset=0.025)
    fake_client.request.side_effect = request_side
    with mock.patch("ntplib.NTPClient", return_value=fake_client):
        assert time_source._refresh_offset() is True

    status = time_source.status()
    assert status["synced"] is True
    assert status["last_sync_server"] == "live.example"
    assert abs(status["offset_seconds"] - 0.025) < 1e-9


def test_refresh_offset_returns_false_when_all_servers_fail() -> None:
    time_source.configure(_cfg(ntp_servers=["a.bad", "b.bad"]))

    fake_client = mock.MagicMock()
    fake_client.request.side_effect = OSError("unreachable")
    with mock.patch("ntplib.NTPClient", return_value=fake_client):
        assert time_source._refresh_offset() is False

    # Synced flag stays False — caller must keep using the system clock.
    assert time_source.status()["synced"] is False


def test_refresh_offset_returns_false_with_empty_server_list() -> None:
    time_source.configure(_cfg(ntp_servers=[]))
    assert time_source._refresh_offset() is False


def test_refresh_offset_returns_false_when_unconfigured() -> None:
    """No configure() call at all — defensive."""
    assert time_source._refresh_offset() is False


# ── status() ──────────────────────────────────────────────────────


def test_status_default_shape_when_unconfigured() -> None:
    s = time_source.status()
    assert s == {
        "enabled": False,
        "synced": False,
        "last_sync_at": 0.0,
        "last_sync_server": "",
        "offset_seconds": 0.0,
        "timezone": "UTC",
    }


def test_status_after_configure_reflects_settings() -> None:
    time_source.configure(_cfg(
        enabled=True,
        timezone_source="manual",
        timezone_manual="America/Chicago",
    ))
    s = time_source.status()
    assert s["enabled"] is True
    assert s["timezone"] == "America/Chicago"
    assert s["synced"] is False  # haven't run a sync yet


# ── start() / stop() ──────────────────────────────────────────────


def test_start_is_noop_when_disabled() -> None:
    time_source.configure(_cfg(enabled=False))
    time_source.start()
    assert time_source._thread is None


def test_start_is_noop_without_configure() -> None:
    time_source.start()
    assert time_source._thread is None
