"""
Weather data cache for GLaDOS.

Stores forecast data fetched by WeatherSubagent in a local JSON file.
Provides human-readable summaries for LLM context injection (both
Engine/ContextBuilder and API wrapper Chat paths).

Single source of truth: the cache file at the configured path.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

# Default cache path (relative to GLaDOS root)
_DEFAULT_CACHE_PATH = Path("data/weather_cache.json")

_lock = threading.Lock()
_cache_path: Path = _DEFAULT_CACHE_PATH
_cached_data: dict[str, Any] | None = None
_cached_prompt: str | None = None


def configure(cache_path: str | Path | None = None) -> None:
    """Set the cache file path. Call once at startup."""
    global _cache_path
    if cache_path:
        _cache_path = Path(cache_path)
    _cache_path.parent.mkdir(parents=True, exist_ok=True)
    # Try to load existing cache from disk
    _load_from_disk()


def update(raw_data: dict[str, Any], weather_config: Any = None) -> None:
    """Update the weather cache with new forecast data.

    Called by WeatherSubagent after each successful fetch.

    Args:
        raw_data: Raw Open-Meteo API response.
        weather_config: WeatherJobConfig for unit labels.
    """
    global _cached_data, _cached_prompt

    try:
        processed = _process_forecast(raw_data, weather_config)
        prompt = _build_prompt(processed)

        with _lock:
            _cached_data = processed
            _cached_prompt = prompt

        # Write to disk (atomic via temp file)
        _write_to_disk(processed)
        logger.debug("Weather cache updated: {}", processed.get("current", {}).get("condition", "unknown"))
    except Exception as exc:
        logger.warning("Weather cache update failed: {}", exc)


def as_prompt() -> str | None:
    """Get the weather summary as an LLM system message.

    Returns None if no weather data is cached.
    Used by ContextBuilder (Engine) and API wrapper (Chat).
    """
    with _lock:
        return _cached_prompt


def get_data() -> dict[str, Any] | None:
    """Get the full cached weather data dict."""
    with _lock:
        return dict(_cached_data) if _cached_data else None


def get_cache_age_seconds() -> float | None:
    """Seconds since last cache update, or None if no cache."""
    with _lock:
        if not _cached_data:
            return None
        updated = _cached_data.get("updated_at")
    if not updated:
        return None
    try:
        dt = datetime.fromisoformat(updated)
        return (datetime.now() - dt).total_seconds()
    except (ValueError, TypeError):
        return None


# ── Internal helpers ─────────────────────────────────────────────────


def _process_forecast(raw: dict[str, Any], config: Any = None) -> dict[str, Any]:
    """Process raw Open-Meteo response into structured cache format."""
    from ..autonomy.agents.weather import WEATHER_CODES

    now = datetime.now()
    unit = "°F"
    wind_unit = "mph"
    if config:
        unit = "°F" if getattr(config, "temperature_unit", "fahrenheit") == "fahrenheit" else "°C"
        wind_unit = getattr(config, "wind_speed_unit", "mph")

    # Current conditions
    current_raw = raw.get("current", {})
    current = {
        "temperature": round(float(current_raw.get("temperature_2m", 0)), 1),
        "wind_speed": round(float(current_raw.get("wind_speed_10m", 0)), 1),
        "humidity": round(float(current_raw.get("relative_humidity_2m", 0))),
        "weather_code": int(current_raw.get("weather_code", -1)),
        "condition": WEATHER_CODES.get(int(current_raw.get("weather_code", -1)), "unknown"),
    }

    # Daily forecast
    daily_raw = raw.get("daily", {})
    daily_dates = daily_raw.get("time", [])
    daily_max = daily_raw.get("temperature_2m_max", [])
    daily_min = daily_raw.get("temperature_2m_min", [])
    daily_codes = daily_raw.get("weather_code", [])

    daily = []
    for i in range(len(daily_dates)):
        code = int(daily_codes[i]) if i < len(daily_codes) else -1
        entry = {
            "date": daily_dates[i],
            "high": round(float(daily_max[i]), 1) if i < len(daily_max) else None,
            "low": round(float(daily_min[i]), 1) if i < len(daily_min) else None,
            "weather_code": code,
            "condition": WEATHER_CODES.get(code, "unknown"),
        }
        daily.append(entry)

    # Today's forecast (first daily entry)
    today = daily[0] if daily else None

    # Weekend forecast
    weekend = _extract_weekend(daily, now)

    # Hourly forecast (next 24 hours)
    hourly_raw = raw.get("hourly", {})
    hourly_times = hourly_raw.get("time", [])
    hourly_temps = hourly_raw.get("temperature_2m", [])
    hourly_codes = hourly_raw.get("weather_code", [])

    hourly_next_24h = []
    for i in range(min(24, len(hourly_times))):
        h_code = int(hourly_codes[i]) if i < len(hourly_codes) else -1
        hourly_next_24h.append({
            "time": hourly_times[i],
            "temperature": round(float(hourly_temps[i]), 1) if i < len(hourly_temps) else None,
            "weather_code": h_code,
            "condition": WEATHER_CODES.get(h_code, "unknown"),
        })

    # Weather alerts (severe codes in forecast)
    alerts = _check_alerts(daily, WEATHER_CODES)

    return {
        "updated_at": now.isoformat(timespec="seconds"),
        "units": {"temperature": unit, "wind_speed": wind_unit},
        "current": current,
        "today": today,
        "daily": daily,
        "weekend": weekend,
        "hourly_next_24h": hourly_next_24h,
        "alerts": alerts,
    }


def _extract_weekend(daily: list[dict], now: datetime) -> dict[str, Any]:
    """Extract upcoming Saturday and Sunday from daily forecast."""
    weekend: dict[str, Any] = {"saturday": None, "sunday": None}
    for day in daily:
        try:
            dt = datetime.fromisoformat(day["date"])
            if dt.weekday() == 5:  # Saturday
                weekend["saturday"] = day
            elif dt.weekday() == 6:  # Sunday
                weekend["sunday"] = day
        except (ValueError, TypeError):
            continue
    return weekend


def _check_alerts(daily: list[dict], weather_codes: dict) -> list[str]:
    """Check for severe weather in the forecast."""
    from ..autonomy.agents.weather import SEVERE_WEATHER_CODES

    alerts = []
    for day in daily:
        code = day.get("weather_code", -1)
        if code in SEVERE_WEATHER_CODES:
            condition = weather_codes.get(code, f"code {code}")
            alerts.append(f"{day.get('date', 'unknown')}: {condition}")
    return alerts


def _build_prompt(data: dict[str, Any]) -> str:
    """Build a concise weather summary for LLM context injection.

    Uses 'degrees' not '°F' so the LLM doesn't copy symbols into TTS output.
    Framed as reference data — the LLM should incorporate it naturally in character,
    not regurgitate it as a data table.
    """
    wind_unit = data.get("units", {}).get("wind_speed", "mph")

    lines = [
        "[Weather reference — use this data when asked about weather. "
        "Do NOT list it back as a table. Weave relevant details into your response naturally.]"
    ]

    # Current
    c = data.get("current", {})
    if c:
        lines.append(
            f"Current: {c['temperature']:.0f} degrees, {c['condition']}, "
            f"wind {c['wind_speed']:.0f} {wind_unit}, humidity {c['humidity']}%."
        )

    # Today
    today = data.get("today")
    if today:
        lines.append(f"Today: high {today['high']:.0f}, low {today['low']:.0f} degrees, {today['condition']}.")

    # Daily forecast
    daily = data.get("daily", [])
    if len(daily) > 1:
        day_strs = []
        for d in daily[1:]:  # Skip today (already shown)
            try:
                dt = datetime.fromisoformat(d["date"])
                day_name = dt.strftime("%A")
            except (ValueError, TypeError):
                day_name = d.get("date", "?")
            day_strs.append(f"{day_name} {d['high']:.0f}/{d['low']:.0f} {d['condition']}")
        lines.append("Week: " + ", ".join(day_strs) + ".")

    # Weekend
    wknd = data.get("weekend", {})
    sat = wknd.get("saturday")
    sun = wknd.get("sunday")
    if sat or sun:
        parts = []
        if sat:
            parts.append(f"Saturday {sat['high']:.0f} degrees {sat['condition']}")
        if sun:
            parts.append(f"Sunday {sun['high']:.0f} degrees {sun['condition']}")
        lines.append("Weekend: " + ", ".join(parts) + ".")

    # Alerts
    alerts = data.get("alerts", [])
    if alerts:
        lines.append("WEATHER ALERTS: " + "; ".join(alerts))

    # Cache age note
    updated = data.get("updated_at", "")
    if updated:
        lines.append(f"(Updated: {updated})")

    return "\n".join(lines)


def _load_from_disk() -> None:
    """Load cached weather data from disk."""
    global _cached_data, _cached_prompt
    try:
        if _cache_path.exists():
            with open(_cache_path, encoding="utf-8") as f:
                data = json.load(f)
            prompt = _build_prompt(data)
            with _lock:
                _cached_data = data
                _cached_prompt = prompt
            logger.debug("Weather cache loaded from disk: {}", _cache_path)
    except Exception as exc:
        logger.warning("Failed to load weather cache from disk: {}", exc)


def _write_to_disk(data: dict[str, Any]) -> None:
    """Write weather cache to disk atomically."""
    tmp_path = _cache_path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        tmp_path.replace(_cache_path)
    except Exception as exc:
        logger.warning("Failed to write weather cache: {}", exc)
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
