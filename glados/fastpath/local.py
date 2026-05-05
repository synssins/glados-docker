"""Local fast-path renderers for time and weather queries.

Two entry points:

- ``try_time(message)`` — returns a literal answer string for time/date
  questions, or ``None`` if the message isn't a time query.
- ``try_weather(message)`` — same shape for weather/forecast questions.

Both functions are pure (no I/O beyond the singletons they read from)
and side-effect free. The api_wrapper hook is responsible for the
persona rewrite, SSE/JSON emit, audit, and conversation-store append.

Design constraints:

- Output is 1-2 sentences. The persona rewriter expects short input
  and produces 1-2 sentences out, matching the ``feedback_short_tier1``
  preference for terse persona-light replies on deterministic queries.
- Time-range parsing is regex/keyword based — sub-millisecond, no
  external calls. Ambiguous phrasings (e.g. "later") default to
  today's forecast per operator preference (2026-05-04 spec sign-off):
  "When uncertain, I would rather see a response along the lines of
  the forecast for the day."
- Phrases that combine a fast-path query with a device command
  (e.g. "what's the weather and turn on the lights") fall through to
  the chat path. Detection: any utterance carrying a known home-command
  verb gates these helpers off (see _has_home_command).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from loguru import logger

from glados.core import time_source, weather_cache
from glados.core.context_gates import needs_time_context, needs_weather_context


# ─────────────────────────────────────────────────────────────────────
# Compound-utterance gate
# ─────────────────────────────────────────────────────────────────────
#
# If the utterance carries a device-control verb in addition to the
# weather/time keyword ("what's the weather AND turn on the lights"),
# the fast-path is the wrong handler — chat needs to route this through
# tool-using lane so both halves get serviced. Conservative gate: any
# of these verbs anywhere in the message → fall through.
_HOME_COMMAND_VERBS: tuple[str, ...] = (
    "turn on", "turn off", "switch on", "switch off",
    "lock", "unlock", "open", "close",
    "set", "dim", "brighten", "raise", "lower",
    "play", "pause", "stop", "resume",
    "arm", "disarm",
    "start", "begin",
)


def _has_home_command(text: str) -> bool:
    low = text.lower()
    return any(v in low for v in _HOME_COMMAND_VERBS)


# ─────────────────────────────────────────────────────────────────────
# Time fast-path
# ─────────────────────────────────────────────────────────────────────


def try_time(message: str) -> Optional[str]:
    """Return a one-line time/date answer, or None to fall through.

    The persona rewriter is invoked downstream — output here is the
    plain factual statement, not the GLaDOS-voiced rendering.
    """
    if not message or not message.strip():
        return None
    if _has_home_command(message):
        return None
    if not needs_time_context(message):
        return None

    now = time_source.now()
    return _format_time_answer(now, message)


def _format_time_answer(now: datetime, message: str) -> str:
    """Render a time/date answer for `now`, biased by what the user asked.

    "What day is it" → date-leaning. "What time" → time-leaning.
    Default → time-leaning with weekday context.
    """
    low = message.lower()

    asks_year = "year" in low
    asks_date = any(kw in low for kw in ("date", "what day", "day is it"))
    asks_time = any(kw in low for kw in (
        "time", "hour", "o'clock", "clock",
    ))

    weekday = now.strftime("%A")
    # %-d / %-I are POSIX-only (no leading zero); Windows strftime
    # raises ValueError on those tokens. Fall back to %d / %I + manual
    # leading-zero strip so tests pass on either platform.
    try:
        date_str = now.strftime("%B %-d, %Y")
    except ValueError:
        date_str = now.strftime("%B %d, %Y").replace(" 0", " ")
    try:
        time_12h = now.strftime("%-I:%M %p")
    except ValueError:
        time_12h = now.strftime("%I:%M %p").lstrip("0")

    if asks_year and not asks_time:
        return f"It is {now.year}."
    if asks_date and not asks_time:
        return f"It is {weekday}, {date_str}."
    if asks_time and not asks_date:
        return f"It is {time_12h}, {weekday}."
    # Combined / ambiguous — give time + weekday (date is implicit in weekday).
    return f"It is {time_12h}, {weekday}."


# ─────────────────────────────────────────────────────────────────────
# Weather fast-path
# ─────────────────────────────────────────────────────────────────────
#
# Time-range kinds the parser can produce:
#
#   current        - now / no temporal qualifier / "later" (per spec
#                    operator preference: ambiguous defaults to today)
#   today          - today's high/low/condition (combined with current)
#   tomorrow       - daily[1]
#   day_offset(N)  - daily[N]; covers "day after tomorrow" (N=2)
#   weekday(W)     - the next occurrence of weekday W (0=Mon..6=Sun)
#   weekend        - Saturday + Sunday from data["weekend"]
#   range(a, b)    - inclusive day range; covers "next N days",
#                    "this week", "later this week", etc.
#   hourly(part)   - "tonight" / "this evening" / "tomorrow morning"
#                    pulls from hourly_next_24h instead of daily
#
# An ambiguous phrase ("weather later") resolves to ``today`` so the
# operator gets a useful answer without a fall-through to chat.

_WEEKDAYS: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Open-Meteo's free API ceiling — matches forecast_days bump in
# WeatherJobConfig. If forecast_days is configured lower, range() output
# is naturally bounded by len(daily) so it degrades gracefully.
_MAX_FORECAST_DAYS = 16


def try_weather(message: str) -> Optional[str]:
    """Return a 1-2 sentence weather answer, or None to fall through."""
    if not message or not message.strip():
        return None
    if _has_home_command(message):
        return None
    if not needs_weather_context(message):
        return None

    data = weather_cache.get_data()
    if not data:
        # No cache yet (first boot, before WeatherSubagent has run).
        # Fall through so the chat path can apologise gracefully rather
        # than us emitting a stub.
        logger.debug("fastpath weather: no cache, falling through")
        return None

    range_kind, range_args = _parse_weather_range(message)
    intent = _parse_weather_intent(message)

    try:
        return _render_weather(data, range_kind, range_args, intent)
    except Exception as exc:
        logger.warning("fastpath weather render failed: {}", exc)
        return None


# ── parsing ──────────────────────────────────────────────────────────


def _parse_weather_intent(message: str) -> str:
    """Classify the user's interest:
       'temperature' | 'conditions' | 'forecast' | 'general'
    """
    low = message.lower()
    if any(kw in low for kw in (
        "temperature", "how hot", "how cold", "how warm", "degrees",
    )):
        return "temperature"
    if any(kw in low for kw in (
        "raining", "snowing", "sunny", "cloudy", "overcast",
        "windy", "storm", "drizzle", "is it going to rain",
        "humid", "humidity",
    )):
        return "conditions"
    if any(kw in low for kw in (
        "forecast", "outlook", "high", "low",
    )):
        return "forecast"
    return "general"


def _parse_weather_range(message: str) -> tuple[str, dict[str, Any]]:
    """Parse a time range from the message.

    Returns (kind, args_dict). ``kind`` is one of 'current', 'today',
    'tomorrow', 'day_offset', 'weekday', 'weekend', 'range', 'hourly'.
    """
    low = message.lower()

    # Hourly-resolution phrases (must come before daily-resolution
    # checks so "tomorrow night" routes to hourly with offset=1 rather
    # than daily-tomorrow).
    if "tonight" in low or "this evening" in low:
        return "hourly", {"offset": 0, "part": "evening"}
    if "tomorrow morning" in low:
        return "hourly", {"offset": 1, "part": "morning"}
    if "tomorrow afternoon" in low:
        return "hourly", {"offset": 1, "part": "afternoon"}
    if "tomorrow night" in low or "tomorrow evening" in low:
        return "hourly", {"offset": 1, "part": "evening"}
    if "this morning" in low:
        return "hourly", {"offset": 0, "part": "morning"}
    if "this afternoon" in low:
        return "hourly", {"offset": 0, "part": "afternoon"}

    # "Day after tomorrow"
    if "day after tomorrow" in low:
        return "day_offset", {"offset": 2}

    # "Tomorrow"
    if "tomorrow" in low:
        return "tomorrow", {}

    # "This weekend" / "the weekend" / "on the weekend"
    if "weekend" in low:
        return "weekend", {}

    # "Next N days" / "next ten days" / "next 10 days" / "in N days"
    m = re.search(
        r"\b(?:next|in|over the next|for the next)\s+"
        r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|thirteen|fourteen|fifteen|sixteen)\s+days?\b",
        low,
    )
    if m:
        n = _word_to_int(m.group(1))
        if n is not None:
            n = max(1, min(n, _MAX_FORECAST_DAYS))
            return "range", {"start": 0, "end": n - 1}

    # "Later this week" / "rest of the week" — must beat the bare
    # "this week" substring check below.
    if "later this week" in low or "rest of the week" in low:
        return "range", {"start": 1, "end": 6}

    # "Next week" / "this week" — full week bracket
    if "next week" in low or "this week" in low:
        return "range", {"start": 0, "end": 6}

    # Specific weekday — "weather Saturday", "what about Tuesday", etc.
    for name, idx in _WEEKDAYS.items():
        # Word-boundary match so "satur" alone doesn't trip.
        if re.search(rf"\b{name}\b", low):
            return "weekday", {"weekday": idx}

    # "Today" — explicit
    if "today" in low or "today's" in low:
        return "today", {}

    # "Later today" — also today (operator-preferred ambiguous default)
    if "later today" in low:
        return "today", {}

    # Bare "later" — operator's spec: default to today's forecast.
    if re.search(r"\blater\b", low):
        return "today", {}

    # No temporal qualifier — current snapshot.
    return "current", {}


def _word_to_int(s: str) -> Optional[int]:
    s = s.lower().strip()
    if s.isdigit():
        return int(s)
    table = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
        "fifteen": 15, "sixteen": 16,
    }
    return table.get(s)


# ── rendering ────────────────────────────────────────────────────────


def _temp_str(value: Any) -> str:
    """Format a temperature value as 'NN degrees' — 'degrees' word
    (rather than the unit symbol) plays better with the TTS pipeline."""
    if value is None:
        return "unknown"
    try:
        return f"{round(float(value))} degrees"
    except (ValueError, TypeError):
        return str(value)


def _render_weather(
    data: dict[str, Any],
    kind: str,
    args: dict[str, Any],
    intent: str,
) -> Optional[str]:
    """Top-level renderer. Dispatches to per-kind helpers."""
    if kind == "current":
        return _render_current(data, intent)
    if kind == "today":
        return _render_today(data, intent)
    if kind == "tomorrow":
        return _render_day_offset(data, 1)
    if kind == "day_offset":
        return _render_day_offset(data, int(args.get("offset", 1)))
    if kind == "weekday":
        return _render_weekday(data, int(args.get("weekday", 0)))
    if kind == "weekend":
        return _render_weekend(data)
    if kind == "range":
        return _render_range(
            data,
            int(args.get("start", 0)),
            int(args.get("end", 6)),
        )
    if kind == "hourly":
        return _render_hourly(
            data,
            int(args.get("offset", 0)),
            str(args.get("part", "evening")),
        )
    return None


def _render_current(data: dict[str, Any], intent: str) -> Optional[str]:
    cur = data.get("current") or {}
    today = data.get("today") or {}
    if not cur:
        return None

    temp = _temp_str(cur.get("temperature"))
    cond = cur.get("condition") or "unknown"

    if intent == "temperature":
        return f"It is currently {temp}."
    if intent == "conditions":
        # "Is it raining" → answer based on current condition.
        return f"It is currently {cond}."
    # general / forecast on a "current"-shaped query → combo with today
    today_high = today.get("high")
    if today_high is not None:
        return (
            f"It is currently {temp} and {cond}. "
            f"Today's high is {_temp_str(today_high)}."
        )
    return f"It is currently {temp} and {cond}."


def _render_today(data: dict[str, Any], intent: str) -> Optional[str]:
    today = data.get("today") or {}
    cur = data.get("current") or {}
    if not today:
        return _render_current(data, intent)

    high = _temp_str(today.get("high"))
    low = _temp_str(today.get("low"))
    cond = today.get("condition") or "unknown"

    # Natural-language framing — "high of N degrees, low of M degrees"
    # rather than "high N, low M" so TTS reads cleanly and the operator
    # gets the explicit unit word on every temperature.
    base = f"Today: high of {high}, low of {low}, {cond}."
    if cur and cur.get("temperature") is not None:
        cur_temp = _temp_str(cur.get("temperature"))
        base += f" Currently {cur_temp}."
    base += _alert_suffix(data)
    return base


def _render_day_offset(data: dict[str, Any], offset: int) -> Optional[str]:
    daily = data.get("daily") or []
    if offset < 0 or offset >= len(daily):
        return None
    day = daily[offset]
    label = _day_label(day, offset)
    high = _temp_str(day.get("high"))
    low = _temp_str(day.get("low"))
    cond = day.get("condition") or "unknown"
    return f"{label}: high of {high}, low of {low}, {cond}."


def _render_weekday(data: dict[str, Any], target_wd: int) -> Optional[str]:
    daily = data.get("daily") or []
    for day in daily:
        try:
            dt = datetime.fromisoformat(day.get("date", ""))
        except (ValueError, TypeError):
            continue
        if dt.weekday() == target_wd:
            name = dt.strftime("%A")
            high = _temp_str(day.get("high"))
            low = _temp_str(day.get("low"))
            cond = day.get("condition") or "unknown"
            return f"{name}: high of {high}, low of {low}, {cond}."
    return None


def _render_weekend(data: dict[str, Any]) -> Optional[str]:
    wknd = data.get("weekend") or {}
    sat = wknd.get("saturday")
    sun = wknd.get("sunday")
    parts: list[str] = []
    if sat:
        parts.append(
            f"Saturday will be {_temp_str(sat.get('high'))} and "
            f"{sat.get('condition', 'unknown')}"
        )
    if sun:
        parts.append(
            f"Sunday will be {_temp_str(sun.get('high'))} and "
            f"{sun.get('condition', 'unknown')}"
        )
    if not parts:
        return None
    return ". ".join(parts) + "."


def _render_range(data: dict[str, Any], start: int, end: int) -> Optional[str]:
    daily = data.get("daily") or []
    if not daily:
        return None
    end = min(end, len(daily) - 1)
    if start > end:
        return None
    # Cap output length: long ranges become unwieldy as TTS. Hard
    # ceiling at 7 days in the rendered string; if the user asked for
    # more, summarise the overflow. Each entry is full-sentence so the
    # TTS reads naturally instead of like a stock-ticker — operator
    # feedback 2026-05-04: "Monday 63 overcast" reads as shorthand;
    # "Monday: 63 degrees and overcast" is the desired shape.
    items: list[str] = []
    rendered_end = min(end, start + 6)
    for i in range(start, rendered_end + 1):
        day = daily[i]
        try:
            dt = datetime.fromisoformat(day.get("date", ""))
            label = dt.strftime("%A")
        except (ValueError, TypeError):
            label = day.get("date", f"day {i}")
        high = _temp_str(day.get("high"))
        cond = day.get("condition") or "unknown"
        items.append(f"{label}: {high}, {cond}")
    out = ". ".join(items) + "."
    if rendered_end < end:
        remaining = end - rendered_end
        out += f" Plus {remaining} more day{'s' if remaining != 1 else ''} after that."
    out += _alert_suffix(data)
    return out


def _render_hourly(
    data: dict[str, Any], offset: int, part: str,
) -> Optional[str]:
    """Render a forecast for a part of a day from hourly_next_24h.

    `offset` is days from today; `part` is morning/afternoon/evening.
    Picks a representative hour and the worst condition observed in
    the window.
    """
    hourly = data.get("hourly_next_24h") or []
    if not hourly:
        return None

    part_hours = {
        "morning": (6, 12),
        "afternoon": (12, 18),
        "evening": (18, 22),
        "night": (22, 24),
    }
    lo, hi = part_hours.get(part, (18, 22))

    # Filter hourly rows that fall in the target day + part window.
    target_temps: list[float] = []
    target_conds: list[str] = []
    for row in hourly:
        try:
            ts = datetime.fromisoformat(row.get("time", ""))
        except (ValueError, TypeError):
            continue
        # Days-from-today: hourly rows are tagged with absolute
        # datetime in the same tz as the cache. Compute "today" as
        # the date of the first hourly row (== now-aligned).
        try:
            base = datetime.fromisoformat(hourly[0].get("time", ""))
        except (ValueError, TypeError):
            continue
        days_from_base = (ts.date() - base.date()).days
        if days_from_base != offset:
            continue
        if not (lo <= ts.hour < hi):
            continue
        t = row.get("temperature")
        if t is not None:
            target_temps.append(float(t))
        c = row.get("condition")
        if c:
            target_conds.append(c)

    if not target_temps and not target_conds:
        # Nothing in the window — fall through.
        return None

    if target_temps:
        avg = sum(target_temps) / len(target_temps)
        temp_str = f"around {round(avg)} degrees"
    else:
        temp_str = ""

    cond_str = _dominant_condition(target_conds) if target_conds else ""

    label = {
        0: f"This {part}",
        1: f"Tomorrow {part}",
    }.get(offset, f"In {offset} days, {part}")

    if temp_str and cond_str:
        return f"{label}: {temp_str}, {cond_str}."
    if temp_str:
        return f"{label}: {temp_str}."
    if cond_str:
        return f"{label}: {cond_str}."
    return None


def _dominant_condition(conds: list[str]) -> str:
    """Return the most-frequent condition, biased toward severe ones
    when present."""
    severe = {
        "thunderstorm", "thunderstorm with hail",
        "heavy rain", "heavy snow", "heavy snow showers",
        "violent rain showers",
    }
    for c in conds:
        if c in severe:
            return c
    # Else pick the most-common.
    counts: dict[str, int] = {}
    for c in conds:
        counts[c] = counts.get(c, 0) + 1
    return max(counts, key=lambda k: counts[k])


def _day_label(day: dict[str, Any], offset: int) -> str:
    """Human-friendly label for a daily forecast entry."""
    if offset == 0:
        return "Today"
    if offset == 1:
        return "Tomorrow"
    try:
        dt = datetime.fromisoformat(day.get("date", ""))
        return dt.strftime("%A")
    except (ValueError, TypeError):
        return f"Day {offset}"


def _alert_suffix(data: dict[str, Any]) -> str:
    """Append a one-line alerts notice if any are present in the cache."""
    alerts = data.get("alerts") or []
    if not alerts:
        return ""
    # Keep it terse — the alerts list contains date+condition pairs.
    first = alerts[0]
    return f" Alert: {first}."
