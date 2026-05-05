"""Tests for ``glados.fastpath.local`` — the time/weather fast-paths.

The fast-paths short-circuit deterministic queries at the precheck
stage. These tests cover:

- Detection (gates on time/weather keywords; compound utterances with
  a home-command verb fall through)
- Time-range parsing for weather (today, tomorrow, day_offset, weekday,
  weekend, range, hourly/part-of-day)
- Ambiguous-phrase fallback to today (operator preference, 2026-05-04)
- Render shapes for the common queries

Tests pin the rendered text to verify the parser and renderer match
the spec; the persona rewriter is invoked downstream and is not
exercised here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from glados.fastpath import local as fp


# ─────────────────────────────────────────────────────────────────────
# Fixture: synthetic weather cache covering a 16-day forecast window
# ─────────────────────────────────────────────────────────────────────


def _fixture_data(today_iso: str = "2026-05-04") -> dict:
    """Return a weather_cache.get_data()-shaped dict.

    `today_iso` anchors the daily forecast. 2026-05-04 is a Monday
    so the weekday helpers can be exercised against known offsets.
    """
    base = datetime.fromisoformat(today_iso)
    daily = []
    weather_codes_cycle = [0, 1, 2, 3, 61, 95, 80, 0, 1, 2, 3, 0, 1, 2, 3, 0]
    cond_cycle = [
        "clear sky", "mainly clear", "partly cloudy", "overcast",
        "light rain", "thunderstorm", "rain showers", "clear sky",
        "mainly clear", "partly cloudy", "overcast", "clear sky",
        "mainly clear", "partly cloudy", "overcast", "clear sky",
    ]
    for i in range(16):
        d = base.replace(day=base.day) if i == 0 else None  # placeholder
        # Use ordinal arithmetic to avoid month-rollover hassle in fixtures.
        from datetime import timedelta
        date = base + timedelta(days=i)
        daily.append({
            "date": date.strftime("%Y-%m-%d"),
            "high": 70 + i,
            "low": 50 + i,
            "weather_code": weather_codes_cycle[i],
            "condition": cond_cycle[i],
        })

    weekend = {"saturday": None, "sunday": None}
    for d in daily:
        dt = datetime.fromisoformat(d["date"])
        if dt.weekday() == 5:
            weekend["saturday"] = d
        elif dt.weekday() == 6 and weekend["sunday"] is None:
            weekend["sunday"] = d

    # 24 hours of hourly entries starting at today midnight, hourly.
    from datetime import timedelta
    hourly = []
    for h in range(24):
        ts = base.replace(hour=h, minute=0, second=0, microsecond=0)
        hourly.append({
            "time": ts.strftime("%Y-%m-%dT%H:%M"),
            "temperature": 60 + h,
            "weather_code": 2 if 18 <= h < 22 else 0,
            "condition": "partly cloudy" if 18 <= h < 22 else "clear sky",
        })

    return {
        "updated_at": today_iso + "T08:00:00",
        "timezone": "America/Chicago",
        "timezone_abbreviation": "CST",
        "units": {"temperature": "°F", "wind_speed": "mph"},
        "current": {
            "temperature": 63,
            "wind_speed": 5,
            "humidity": 60,
            "weather_code": 3,
            "condition": "overcast",
        },
        "today": daily[0],
        "daily": daily,
        "weekend": weekend,
        "hourly_next_24h": hourly,
        "alerts": [],
    }


@pytest.fixture
def weather_data(monkeypatch):
    data = _fixture_data()
    monkeypatch.setattr(
        "glados.core.weather_cache.get_data", lambda: data,
    )
    return data


# ─────────────────────────────────────────────────────────────────────
# Time fast-path
# ─────────────────────────────────────────────────────────────────────


def test_try_time_returns_none_for_non_time_query():
    assert fp.try_time("turn on the lights") is None
    assert fp.try_time("tell me a joke") is None
    assert fp.try_time("") is None


def test_try_time_detects_time_query(monkeypatch):
    fixed = datetime(2026, 5, 4, 13, 58, tzinfo=timezone.utc)
    monkeypatch.setattr("glados.core.time_source.now", lambda: fixed)
    out = fp.try_time("what time is it")
    assert out is not None
    assert "1:58 PM" in out or "1:58 pm" in out.lower()
    assert "Monday" in out


def test_try_time_date_query(monkeypatch):
    fixed = datetime(2026, 5, 4, 13, 58, tzinfo=timezone.utc)
    monkeypatch.setattr("glados.core.time_source.now", lambda: fixed)
    out = fp.try_time("what's the date")
    assert out is not None
    assert "Monday" in out
    assert "2026" in out


def test_try_time_year_only(monkeypatch):
    fixed = datetime(2026, 5, 4, 13, 58, tzinfo=timezone.utc)
    monkeypatch.setattr("glados.core.time_source.now", lambda: fixed)
    out = fp.try_time("what year is it")
    assert out == "It is 2026."


def test_try_time_compound_with_command_falls_through(monkeypatch):
    fixed = datetime(2026, 5, 4, 13, 58, tzinfo=timezone.utc)
    monkeypatch.setattr("glados.core.time_source.now", lambda: fixed)
    # "what time should I turn on the lights" carries 'turn on' — chat
    # path should handle compound intent.
    assert fp.try_time("what time should I turn on the lights") is None


# ─────────────────────────────────────────────────────────────────────
# Weather fast-path — gating
# ─────────────────────────────────────────────────────────────────────


def test_try_weather_returns_none_for_non_weather(weather_data):
    assert fp.try_weather("tell me a joke") is None
    assert fp.try_weather("") is None


def test_try_weather_falls_through_with_home_command(weather_data):
    # Compound: weather AND device command → chat path needed.
    assert fp.try_weather("what's the weather and turn on the lights") is None


def test_try_weather_falls_through_when_cache_empty(monkeypatch):
    monkeypatch.setattr("glados.core.weather_cache.get_data", lambda: None)
    assert fp.try_weather("what's the weather") is None


# ─────────────────────────────────────────────────────────────────────
# Weather fast-path — current / general
# ─────────────────────────────────────────────────────────────────────


def test_weather_current_temperature_only(weather_data):
    out = fp.try_weather("what's the temperature outside")
    assert out is not None
    assert "63 degrees" in out


def test_weather_current_general(weather_data):
    out = fp.try_weather("what's the weather")
    assert out is not None
    assert "63 degrees" in out
    assert "overcast" in out
    # Should also include today's high (combo for general queries).
    assert "70" in out


def test_weather_is_it_raining(weather_data):
    out = fp.try_weather("is it raining")
    assert out is not None
    # Current condition is "overcast" in the fixture.
    assert "overcast" in out


# ─────────────────────────────────────────────────────────────────────
# Weather fast-path — today / tomorrow / day_offset
# ─────────────────────────────────────────────────────────────────────


def test_weather_today_explicit(weather_data):
    out = fp.try_weather("what is the forecast today")
    assert out is not None
    assert out.startswith("Today")
    assert "70" in out  # high
    assert "50" in out  # low


def test_weather_tomorrow(weather_data):
    out = fp.try_weather("what's the weather tomorrow")
    assert out is not None
    assert out.startswith("Tomorrow")
    assert "71" in out  # tomorrow's high


def test_weather_day_after_tomorrow(weather_data):
    out = fp.try_weather("what's the weather day after tomorrow")
    assert out is not None
    # Day index 2 in fixture: high=72, weekday=Wednesday (2026-05-06)
    assert "72" in out
    assert "Wednesday" in out


# ─────────────────────────────────────────────────────────────────────
# Weather fast-path — weekday / weekend / range
# ─────────────────────────────────────────────────────────────────────


def test_weather_specific_weekday(weather_data):
    # 2026-05-04 is Monday; Saturday is 2026-05-09 → daily[5] in fixture
    # → high=75, condition="thunderstorm". Phrasing must include a
    # weather keyword to trip the gate; bare "what about Saturday" is
    # anaphoric and out of v1 scope (chat path handles that case).
    out = fp.try_weather("what's the weather Saturday")
    assert out is not None
    assert "Saturday" in out
    assert "75" in out


def test_weather_weekend(weather_data):
    out = fp.try_weather("what's the weather this weekend")
    assert out is not None
    assert "Saturday" in out
    assert "Sunday" in out


def test_weather_next_ten_days(weather_data):
    out = fp.try_weather("what's the forecast for the next ten days")
    assert out is not None
    # Output is capped at 7 rendered days; the rest are summarised.
    # Verify the day-7 cutoff message appears.
    assert "more day" in out or "days after" in out


def test_weather_next_three_days(weather_data):
    out = fp.try_weather("what's the weather for the next three days")
    assert out is not None
    # All three days should be inline (no cutoff).
    assert "more day" not in out


def test_weather_this_week(weather_data):
    out = fp.try_weather("what's the weather this week")
    assert out is not None
    # Should be a multi-day rendering.
    assert "70" in out


# ─────────────────────────────────────────────────────────────────────
# Weather fast-path — hourly (part of day)
# ─────────────────────────────────────────────────────────────────────


def test_weather_tonight(weather_data):
    out = fp.try_weather("what's the weather tonight")
    assert out is not None
    assert "evening" in out.lower() or "tonight" in out.lower() or "this" in out.lower()
    # Fixture has temps 60..83 across the day; evening (18-22) avg ~79.
    # Just verify a number appears.
    assert any(ch.isdigit() for ch in out)


# ─────────────────────────────────────────────────────────────────────
# Weather fast-path — ambiguous phrasings (operator-preferred default)
# ─────────────────────────────────────────────────────────────────────


def test_weather_bare_later_defaults_to_today(weather_data):
    # Operator spec 2026-05-04: "When uncertain, I would rather see a
    # response along the lines of the forecast for the day."
    out = fp.try_weather("what about the weather later")
    assert out is not None
    assert out.startswith("Today")


def test_weather_later_today_is_today(weather_data):
    out = fp.try_weather("what's the weather later today")
    assert out is not None
    assert out.startswith("Today")


def test_weather_later_this_week_is_range(weather_data):
    out = fp.try_weather("what's the weather later this week")
    assert out is not None
    # "later this week" = days 1..6, so output should NOT start with Today
    # and SHOULD include multiple weekday names.
    assert not out.startswith("Today")


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


def test_word_to_int_handles_words_and_digits():
    assert fp._word_to_int("3") == 3
    assert fp._word_to_int("ten") == 10
    assert fp._word_to_int("sixteen") == 16
    assert fp._word_to_int("nope") is None


def test_intent_classifier():
    assert fp._parse_weather_intent("what's the temperature") == "temperature"
    assert fp._parse_weather_intent("is it raining") == "conditions"
    assert fp._parse_weather_intent("what's the forecast") == "forecast"
    assert fp._parse_weather_intent("what's the weather") == "general"


def test_range_parser_explicit_count():
    kind, args = fp._parse_weather_range("forecast for the next ten days")
    assert kind == "range"
    assert args == {"start": 0, "end": 9}


def test_range_parser_weekday():
    kind, args = fp._parse_weather_range("what about Saturday")
    assert kind == "weekday"
    assert args["weekday"] == 5  # Sat


def test_range_parser_no_qualifier_is_current():
    kind, _ = fp._parse_weather_range("what's the weather")
    assert kind == "current"


def test_range_parser_today():
    kind, _ = fp._parse_weather_range("what is the forecast today")
    assert kind == "today"


def test_range_parser_tomorrow():
    kind, _ = fp._parse_weather_range("weather tomorrow")
    assert kind == "tomorrow"


def test_range_parser_day_after_tomorrow_beats_tomorrow():
    # The "day after tomorrow" check must come before the bare
    # "tomorrow" substring or the parser would mis-classify.
    kind, args = fp._parse_weather_range("weather day after tomorrow")
    assert kind == "day_offset"
    assert args["offset"] == 2


def test_range_parser_tonight():
    kind, args = fp._parse_weather_range("weather tonight")
    assert kind == "hourly"
    assert args["offset"] == 0
    assert args["part"] == "evening"


def test_range_parser_tomorrow_morning_is_hourly():
    kind, args = fp._parse_weather_range("weather tomorrow morning")
    assert kind == "hourly"
    assert args["offset"] == 1
    assert args["part"] == "morning"


def test_range_parser_bare_later_is_today():
    # Operator-spec: ambiguous "later" defaults to today.
    kind, _ = fp._parse_weather_range("weather later")
    assert kind == "today"


def test_range_parser_later_this_week_is_range_offset_1():
    kind, args = fp._parse_weather_range("weather later this week")
    assert kind == "range"
    assert args["start"] == 1


def test_compound_with_home_verb_returns_none(weather_data):
    # Has 'turn on'.
    assert fp.try_weather("what's the weather and turn on the lights") is None
    # Has 'lock'.
    assert fp.try_weather("is it raining and lock the door") is None
