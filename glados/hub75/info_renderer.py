"""
Info panel renderer for the HUB75 display.

Uses Pillow to render time, weather, and home status text onto a 64×64
RGB image.  Returns raw bytes compatible with the DDP sender.

Design principles:
  - Muted colours (dim amber, dim blue) to avoid drawing attention
  - Only 1–2 FPS update rate (content changes slowly)
  - Text scaled for 64×64 LED matrix legibility

This module is scaffolding for a future "always-on" info panel mode.
Currently not wired into the render loop — enable via hub75.yaml
``info_panel.enabled: true`` once the display controller supports it.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from PIL import Image, ImageDraw, ImageFont

# ── Colour constants (RGB, all kept dim for USB power) ─────────
COLOR_TIME = (180, 120, 40)       # Dim warm amber
COLOR_DAY = (120, 80, 30)         # Dimmer amber
COLOR_TEMP = (140, 90, 25)        # Warm amber
COLOR_CONDITION = (40, 70, 140)   # Dim blue
COLOR_HILO = (60, 60, 80)         # Muted blue-gray
COLOR_OK = (15, 60, 15)           # Dim green — locked/closed
COLOR_ALERT = (140, 35, 0)        # Dim red-orange — open/unlocked
COLOR_SEPARATOR = (25, 25, 25)    # Nearly invisible gray
COLOR_BG = (0, 0, 0)              # Pure black background
COLOR_LABEL = (50, 50, 50)        # Dim gray for entity labels

# ── Font paths (Consolas — crisp monospace, good for LEDs) ─────
_FONT_PATH = "C:/Windows/Fonts/consola.ttf"
_FONT_BOLD = "C:/Windows/Fonts/consolab.ttf"

# Cached font instances (populated on first use)
_fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Return a cached Consolas font at the given pixel size."""
    path = _FONT_BOLD if bold else _FONT_PATH
    key = (path, size)
    if key not in _fonts:
        try:
            _fonts[key] = ImageFont.truetype(path, size)
        except OSError:
            logger.warning("HUB75 info: font {} not found, using default", path)
            _fonts[key] = ImageFont.load_default()
    return _fonts[key]


def _dim(
    colour: tuple[int, int, int],
    brightness: float,
) -> tuple[int, int, int]:
    """Apply a brightness multiplier to an RGB colour."""
    return (
        int(colour[0] * brightness),
        int(colour[1] * brightness),
        int(colour[2] * brightness),
    )


# ── Weather condition shortener ────────────────────────────────

_CONDITION_SHORT: dict[str, str] = {
    "clear sky": "CLEAR",
    "mainly clear": "CLEAR",
    "partly cloudy": "PT CLOUD",
    "overcast": "OVERCAST",
    "light rain": "LT RAIN",
    "moderate rain": "RAIN",
    "heavy rain": "HVY RAIN",
    "light snow": "LT SNOW",
    "moderate snow": "SNOW",
    "heavy snow": "HVY SNOW",
    "light drizzle": "DRIZZLE",
    "moderate drizzle": "DRIZZLE",
    "dense drizzle": "DRIZZLE",
    "thunderstorm": "TSTORM",
    "fog": "FOG",
    "depositing rime fog": "FOG",
    "freezing rain": "FRZ RAIN",
    "freezing drizzle": "FRZ DRZL",
    "rain showers": "SHOWERS",
    "snow showers": "SNOW SHR",
    "snow grains": "SNOW",
}


def _shorten_condition(condition: str) -> str:
    """Shorten a weather condition string to ≤10 chars for the panel."""
    return _CONDITION_SHORT.get(condition.lower(), condition[:10].upper())


# ── Thread-safe data container ─────────────────────────────────


class InfoPanelData:
    """Thread-safe container for info panel display data.

    Written by :class:`InfoFetcher` (background thread),
    read by the render loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._weather: dict[str, Any] | None = None
        self._home: dict[str, str] = {}  # entity_id → state string

    def update_weather(self, data: dict[str, Any]) -> None:
        """Store latest weather cache data."""
        with self._lock:
            self._weather = data

    def update_home(self, states: dict[str, str]) -> None:
        """Store latest HA entity states."""
        with self._lock:
            self._home = dict(states)

    def get_weather(self) -> dict[str, Any] | None:
        """Return a snapshot of the weather data (or None)."""
        with self._lock:
            return dict(self._weather) if self._weather else None

    def get_home(self) -> dict[str, str]:
        """Return a snapshot of home entity states."""
        with self._lock:
            return dict(self._home)


# ── Main renderer ──────────────────────────────────────────────


def render_info_frame(
    data: InfoPanelData,
    panel_w: int = 64,
    panel_h: int = 64,
    brightness: float = 0.4,
) -> bytes:
    """Render the info panel as raw RGB bytes.

    Layout (64×64 pixels):

    ::

        Rows  0–15 : TIME  — "1:47 PM" (bold 16pt) + "MON" (8pt)
        Row   23   : thin gray separator
        Rows 25–54 : WEATHER — "57°F" (bold 14pt) + "CLEAR" (8pt)
                     + "H57 L41" (8pt)
        Row   56   : thin gray separator
        Rows 58–63 : HOME — coloured status dots + 2-char labels

    Args:
        data: Current info data from :class:`InfoPanelData`.
        panel_w: Panel width in pixels.
        panel_h: Panel height in pixels.
        brightness: Global brightness multiplier (0.0–1.0).

    Returns:
        ``panel_w * panel_h * 3`` raw RGB bytes (row-major).
    """
    img = Image.new("RGB", (panel_w, panel_h), COLOR_BG)
    draw = ImageDraw.Draw(img)
    now = datetime.now()

    # ── Zone 1: Time (rows 0–15) ─────────────────────────
    time_str = now.strftime("%I:%M").lstrip("0")  # "1:47"
    ampm = now.strftime("%p")                       # "PM"
    day_str = now.strftime("%a").upper()             # "MON"

    f_time = _font(16, bold=True)
    f_small = _font(8)

    bbox = draw.textbbox((0, 0), time_str, font=f_time)
    tw = bbox[2] - bbox[0]
    tx = (panel_w - tw) // 2 - 4  # shift left for AM/PM space
    draw.text((tx, 0), time_str, fill=_dim(COLOR_TIME, brightness), font=f_time)

    # AM/PM beside the time
    draw.text((tx + tw + 1, 4), ampm, fill=_dim(COLOR_DAY, brightness), font=f_small)

    # Day of week centred below time
    bbox_day = draw.textbbox((0, 0), day_str, font=f_small)
    dw = bbox_day[2] - bbox_day[0]
    draw.text(((panel_w - dw) // 2, 14), day_str, fill=_dim(COLOR_DAY, brightness), font=f_small)

    # Separator
    draw.line([(4, 23), (panel_w - 5, 23)], fill=_dim(COLOR_SEPARATOR, brightness))

    # ── Zone 2: Weather (rows 25–54) ─────────────────────
    weather = data.get_weather()
    if weather:
        current = weather.get("current", {})
        temp = current.get("temperature", "?")
        condition = current.get("condition", "")
        today = weather.get("today", {})
        hi = today.get("high", "?")
        lo = today.get("low", "?")

        # Temperature — large
        f_temp = _font(14, bold=True)
        temp_str = f"{temp:.0f}F" if isinstance(temp, (int, float)) else str(temp)
        bbox_t = draw.textbbox((0, 0), temp_str, font=f_temp)
        ttw = bbox_t[2] - bbox_t[0]
        draw.text(
            ((panel_w - ttw) // 2, 25), temp_str,
            fill=_dim(COLOR_TEMP, brightness), font=f_temp,
        )

        # Condition — short text
        cond_short = _shorten_condition(condition)
        f_cond = _font(8)
        bbox_c = draw.textbbox((0, 0), cond_short, font=f_cond)
        cw = bbox_c[2] - bbox_c[0]
        draw.text(
            ((panel_w - cw) // 2, 38), cond_short,
            fill=_dim(COLOR_CONDITION, brightness), font=f_cond,
        )

        # High / Low
        if isinstance(hi, (int, float)) and isinstance(lo, (int, float)):
            hilo_str = f"H{hi:.0f} L{lo:.0f}"
        else:
            hilo_str = "---"
        bbox_hl = draw.textbbox((0, 0), hilo_str, font=f_cond)
        hlw = bbox_hl[2] - bbox_hl[0]
        draw.text(
            ((panel_w - hlw) // 2, 47), hilo_str,
            fill=_dim(COLOR_HILO, brightness), font=f_cond,
        )
    else:
        f_msg = _font(8)
        draw.text((6, 32), "NO WEATHER", fill=_dim(COLOR_CONDITION, brightness), font=f_msg)

    # Separator
    draw.line([(4, 56), (panel_w - 5, 56)], fill=_dim(COLOR_SEPARATOR, brightness))

    # ── Zone 3: Home status (rows 58–63) ─────────────────
    home = data.get_home()
    _draw_home_status(draw, home, y_start=58, panel_w=panel_w, brightness=brightness)

    return img.tobytes()


def _draw_home_status(
    draw: ImageDraw.ImageDraw,
    home: dict[str, str],
    y_start: int,
    panel_w: int,
    brightness: float,
) -> None:
    """Draw compact home-status indicators as coloured dots + labels.

    Each entity gets a 3×3 coloured square (green = OK, red = alert)
    with a 2-character label below it.
    """
    f = _font(7)

    # Default entities — overridden when config drives this via InfoFetcher
    entities: list[tuple[str, str, str]] = [
        ("cover.garage_door", "G1", "closed"),
        ("cover.garage_door_1", "G2", "closed"),
        ("lock.yale_smart_lock_with_matter", "LK", "locked"),
        ("binary_sensor.front_door_contact_sensor_contact", "FD", "off"),
        ("binary_sensor.back_door_contact_sensor_contact", "BD", "off"),
    ]

    x = 2
    for entity_id, label, ok_state in entities:
        state = home.get(entity_id, "unknown")
        is_ok = state == ok_state or state == "unknown"
        colour = COLOR_OK if is_ok else COLOR_ALERT
        dot_colour = _dim(colour, brightness)

        # 3×3 status square
        draw.rectangle([(x, y_start), (x + 2, y_start + 2)], fill=dot_colour)
        # 2-char label below
        draw.text((max(0, x - 1), y_start + 3), label, fill=_dim(COLOR_LABEL, brightness), font=f)
        x += 13  # spacing between indicators
