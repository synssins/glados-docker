"""
Weather subagent for GLaDOS autonomy system.

Periodically fetches weather data and alerts on significant changes.
All thresholds and unit settings are read from WeatherJobConfig
(single source of truth in glados_config.yaml).
"""

from __future__ import annotations

import httpx
from loguru import logger

from ...core.llm_decision import LLMConfig, LLMDecisionError, UrgencyDecision, llm_decide_sync
from ...core import weather_cache
from ..config import WeatherJobConfig
from ..subagent import Subagent, SubagentConfig, SubagentOutput

WEATHER_CODES: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "light rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "light snow",
    73: "moderate snow",
    75: "heavy snow",
    80: "rain showers",
    81: "rain showers",
    82: "violent rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}

SEVERE_WEATHER_CODES = {82, 86, 95, 96, 99}


class WeatherSubagent(Subagent):
    """
    Subagent that monitors weather conditions.

    Fetches current weather at configured intervals and notifies on
    significant temperature changes, severe weather, or high winds.
    All settings (units, thresholds, timeouts) come from WeatherJobConfig.
    """

    def __init__(
        self,
        config: SubagentConfig,
        weather_config: WeatherJobConfig,
        llm_config: LLMConfig | None = None,
        **kwargs,
    ) -> None:
        super().__init__(config, **kwargs)
        self._wc = weather_config
        self._llm_config = llm_config
        self._last_temp: float | None = None
        self._last_code: int | None = None

    def tick(self) -> SubagentOutput | None:
        """Fetch and analyze current weather."""
        if self._wc.latitude is None or self._wc.longitude is None:
            return SubagentOutput(
                status="error",
                summary="Weather disabled: location not set",
                notify_user=False,
            )

        data = self._fetch_weather()
        if not data:
            return SubagentOutput(
                status="error",
                summary="Weather fetch failed",
                notify_user=False,
            )

        # Update the weather cache (makes data available to Chat path)
        weather_cache.update(data, self._wc)

        current = data.get("current", {})
        temp = float(current.get("temperature_2m", 0.0))
        wind = float(current.get("wind_speed_10m", 0.0))
        humidity = float(current.get("relative_humidity_2m", 0.0))
        code = int(current.get("weather_code", -1))
        condition = WEATHER_CODES.get(code, f"code {code}")
        unit = "°F" if self._wc.temperature_unit == "fahrenheit" else "°C"
        wind_unit = self._wc.wind_speed_unit
        summary = f"{condition}, {temp:.0f}{unit}, wind {wind:.0f} {wind_unit}"

        # Use LLM to evaluate weather urgency if config available
        alerts: list[str] = []
        if self._llm_config:
            try:
                temp_change_info = ""
                if self._last_temp is not None:
                    change = temp - self._last_temp
                    temp_change_info = f", temperature changed {change:+.1f}{unit} since last check"

                decision = llm_decide_sync(
                    prompt="Evaluate this weather for user notification: {weather}",
                    context={
                        "weather": (
                            f"Condition: {condition}, Temperature: {temp:.0f}{unit}, "
                            f"Wind: {wind:.0f} {wind_unit}, Humidity: {humidity:.0f}%{temp_change_info}"
                        ),
                    },
                    schema=UrgencyDecision,
                    config=self._llm_config,
                    system_prompt=(
                        "You evaluate weather conditions for a voice assistant. "
                        f"Notify users about: severe weather (thunderstorms, violent rain, hail), "
                        f"high winds ({self._wc.wind_alert_mph}+ {wind_unit}), or significant "
                        f"temperature changes ({self._wc.temp_change_f}+ degrees). "
                        "Set importance 0.0-1.0 based on urgency. Be concise in your reason."
                    ),
                )
                notify_user = decision.notify_user
                importance = decision.importance
                if decision.reason:
                    alerts.append(decision.reason)
            except LLMDecisionError as e:
                logger.warning("WeatherSubagent: LLM decision failed, using fallback: %s", e)
                notify_user, importance, alerts = self._fallback_heuristics(code, condition, wind, temp)
        else:
            # No LLM config - use fallback heuristics
            notify_user, importance, alerts = self._fallback_heuristics(code, condition, wind, temp)

        self._last_temp = temp
        self._last_code = code

        # Check forecast for incoming severe weather (next 7 days)
        cache_data = weather_cache.get_data()
        if cache_data:
            forecast_alerts = cache_data.get("alerts", [])
            for fa in forecast_alerts:
                if fa not in alerts:
                    alerts.append(f"Forecast: {fa}")
                    if not notify_user:
                        notify_user = True
                        importance = max(importance, 0.7)

        # Generate detailed report when there's something notable
        report = None
        if alerts or importance >= 0.5:
            report = self._generate_report(data, current, temp, wind, humidity, condition, alerts)

        return SubagentOutput(
            status="done",
            summary=summary,
            report=report,
            notify_user=notify_user,
            importance=importance,
            confidence=0.7,
            next_run=self._config.loop_interval_s,
        )

    def _generate_report(
        self,
        data: dict,
        current: dict,
        temp: float,
        wind: float,
        humidity: float,
        condition: str,
        alerts: list[str],
    ) -> str:
        """Generate detailed weather report."""
        unit = "°F" if self._wc.temperature_unit == "fahrenheit" else "°C"
        wind_unit = self._wc.wind_speed_unit
        lines = ["## Weather Report", ""]

        # Alerts section
        if alerts:
            lines.append("### Alerts")
            for alert in alerts:
                lines.append(f"- {alert}")
            lines.append("")

        # Current conditions
        lines.append("### Current Conditions")
        lines.append(f"- **Condition:** {condition}")
        lines.append(f"- **Temperature:** {temp:.0f}{unit}")
        lines.append(f"- **Wind:** {wind:.0f} {wind_unit}")
        lines.append(f"- **Humidity:** {humidity:.0f}%")
        lines.append("")

        # Hourly forecast (next 6 hours)
        hourly = data.get("hourly", {})
        hourly_temps = hourly.get("temperature_2m", [])
        hourly_codes = hourly.get("weather_code", [])
        hourly_times = hourly.get("time", [])

        if hourly_temps and hourly_codes and len(hourly_temps) >= 6:
            lines.append("### Next 6 Hours")
            for i in range(6):
                time_str = hourly_times[i].split("T")[1] if i < len(hourly_times) else f"+{i}h"
                h_temp = hourly_temps[i]
                h_code = hourly_codes[i]
                h_cond = WEATHER_CODES.get(h_code, f"code {h_code}")
                lines.append(f"- {time_str}: {h_temp:.0f}{unit}, {h_cond}")
            lines.append("")

        # Daily forecast (if available)
        daily = data.get("daily", {})
        daily_max = daily.get("temperature_2m_max", [])
        daily_min = daily.get("temperature_2m_min", [])
        daily_codes = daily.get("weather_code", [])
        daily_dates = daily.get("time", [])

        if daily_max and daily_min and len(daily_max) >= 3:
            lines.append("### 3-Day Outlook")
            for i in range(min(3, len(daily_max))):
                date_str = daily_dates[i] if i < len(daily_dates) else f"Day {i+1}"
                d_max = daily_max[i]
                d_min = daily_min[i]
                d_code = daily_codes[i] if i < len(daily_codes) else -1
                d_cond = WEATHER_CODES.get(d_code, "")
                lines.append(f"- {date_str}: {d_min:.0f}-{d_max:.0f}{unit}, {d_cond}")

        return "\n".join(lines)

    def _fetch_weather(self) -> dict[str, object] | None:
        """Fetch current weather from Open-Meteo API.

        Phase 6.4 (2026-04-22): lat/long/units/timezone now come from
        the consolidated cfg.global_.weather so the WebUI Weather tab
        is the single source of truth. Previously these were split
        between WeatherJobConfig and WeatherGlobal, leading to UI-vs-
        fetcher drift. Autonomy-specific tuning (forecast_days,
        fetch_timeout_s, alert thresholds) stays on WeatherJobConfig
        since those aren't operator-facing. Falls back to WeatherJobConfig
        values if the global config somehow isn't loaded yet.
        """
        from ...core.config_store import cfg as _cfg
        try:
            gw = _cfg.global_.weather
            lat = gw.latitude if gw.latitude else self._wc.latitude
            lng = gw.longitude if gw.longitude else self._wc.longitude
            t_unit = gw.temperature_unit
            w_unit = gw.wind_speed_unit
            p_unit = gw.precipitation_unit
            tz = gw.timezone
        except Exception:
            lat = self._wc.latitude
            lng = self._wc.longitude
            t_unit = self._wc.temperature_unit
            w_unit = self._wc.wind_speed_unit
            p_unit = self._wc.precipitation_unit
            tz = self._wc.timezone
        params = {
            "latitude": lat,
            "longitude": lng,
            "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
            "hourly": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "forecast_days": self._wc.forecast_days,
            "timezone": tz,
            "temperature_unit": t_unit,
            "wind_speed_unit": w_unit,
            "precipitation_unit": p_unit,
        }
        try:
            response = httpx.get(
                "https://api.open-meteo.com/v1/forecast",
                params=params,
                timeout=self._wc.fetch_timeout_s,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.warning("WeatherSubagent: failed to fetch weather: %s", exc)
            return None

    def _fallback_heuristics(
        self, code: int, condition: str, wind: float, temp: float
    ) -> tuple[bool, float, list[str]]:
        """Fallback heuristics when LLM is unavailable."""
        unit = "°F" if self._wc.temperature_unit == "fahrenheit" else "°C"
        wind_unit = self._wc.wind_speed_unit
        notify_user = False
        importance = 0.2
        alerts: list[str] = []

        if code in SEVERE_WEATHER_CODES:
            notify_user = True
            importance = max(importance, 0.8)
            alerts.append(f"Severe weather: {condition}")

        if wind >= self._wc.wind_alert_mph:
            notify_user = True
            importance = max(importance, 0.7)
            alerts.append(f"High winds: {wind:.0f} {wind_unit}")

        if self._last_temp is not None and abs(temp - self._last_temp) >= self._wc.temp_change_f:
            notify_user = True
            importance = max(importance, 0.6)
            change = temp - self._last_temp
            direction = "risen" if change > 0 else "dropped"
            alerts.append(f"Temperature {direction} {abs(change):.1f}{unit}")

        return notify_user, importance, alerts
