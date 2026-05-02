"""Weather cache processed-forecast shape tests.

Covers the pieces of ``glados.core.weather_cache._process_forecast`` that
non-weather consumers depend on. The Time Source feature reads the
resolved IANA timezone from the cache rather than calling Open-Meteo
geocoding itself, so this file pins the timezone-capture path.
"""
from __future__ import annotations

from glados.core.weather_cache import _process_forecast


def _minimal_raw(**overrides) -> dict:
    """Smallest Open-Meteo-shaped response that ``_process_forecast``
    accepts without raising. Use ``overrides`` to add fields under test."""
    base = {
        "current": {
            "temperature_2m": 70.0,
            "wind_speed_10m": 5.0,
            "relative_humidity_2m": 50,
            "weather_code": 0,
        },
        "daily": {
            "time": [],
            "temperature_2m_max": [],
            "temperature_2m_min": [],
            "weather_code": [],
        },
        "hourly": {
            "time": [],
            "temperature_2m": [],
            "weather_code": [],
        },
    }
    base.update(overrides)
    return base


def test_process_forecast_captures_resolved_timezone() -> None:
    raw = _minimal_raw(
        timezone="America/Chicago",
        timezone_abbreviation="CST",
    )
    processed = _process_forecast(raw)
    assert processed["timezone"] == "America/Chicago"
    assert processed["timezone_abbreviation"] == "CST"


def test_process_forecast_timezone_missing_is_none() -> None:
    """Older cache files written before timezone capture must keep
    parsing — missing fields surface as None, not KeyError."""
    processed = _process_forecast(_minimal_raw())
    assert processed["timezone"] is None
    assert processed["timezone_abbreviation"] is None
