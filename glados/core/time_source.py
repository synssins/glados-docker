"""Authoritative wall-clock time for GLaDOS.

The container's system clock drifts; on a host VM that gets suspended
it can be wildly wrong. This module syncs an offset against an NTP
server (NIST default) at startup and on a refresh interval, then
exposes a tz-aware datetime for the chat path to inject when the user
asks about the time.

Timezone is derived from the operator's weather coordinates (Open-Meteo
returns the resolved IANA zone in the forecast response, captured into
``weather_cache`` by ``_process_forecast``) or from a manual override
in ``TimeGlobal.timezone_manual``. DST is handled automatically by
Python's stdlib ``zoneinfo`` as long as the IANA name is correct.

NTP unreachable falls back to the system clock with a WARNING log —
better to give an answer (potentially slightly off) than to drop the
injection entirely. Operators see the unsynchronized state on the
System → Time card so misalignment isn't silent.
"""

from __future__ import annotations

import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from loguru import logger

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover — zoneinfo is stdlib on >=3.9
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment]


@dataclass
class _SyncState:
    offset_seconds: float = 0.0
    last_sync_at: float = 0.0
    last_sync_server: str = ""
    synced: bool = False


@dataclass
class _RuntimeConfig:
    enabled: bool
    ntp_servers: list[str]
    refresh_interval_s: float
    timezone_source: str
    timezone_manual: str
    weather_cache_getter: Optional[Callable[[], Optional[dict[str, Any]]]] = None


_state: _SyncState = _SyncState()
_lock = threading.Lock()
_config: Optional[_RuntimeConfig] = None
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def configure(
    time_cfg: Any,
    weather_cache_getter: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
) -> None:
    """Configure the time_source from a ``TimeGlobal`` pydantic model
    and a callable returning the weather_cache data dict (or None).

    Idempotent. Safe to call again on config reload — replaces the
    runtime config in place; the running sync thread picks up the new
    server list / interval on its next iteration.
    """
    global _config
    refresh = max(60.0, float(getattr(time_cfg, "refresh_interval_hours", 6.0)) * 3600.0)
    _config = _RuntimeConfig(
        enabled=bool(getattr(time_cfg, "enabled", True)),
        ntp_servers=list(getattr(time_cfg, "ntp_servers", []) or []),
        refresh_interval_s=refresh,
        timezone_source=str(getattr(time_cfg, "timezone_source", "auto")),
        timezone_manual=str(getattr(time_cfg, "timezone_manual", "")),
        weather_cache_getter=weather_cache_getter,
    )


def start() -> None:
    """Spawn the background sync thread. Call once at engine init,
    after ``configure()``. No-op if disabled or already running."""
    global _thread
    if _config is None or not _config.enabled:
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_sync_loop, name="time-source-ntp", daemon=True)
    _thread.start()


def stop() -> None:
    """Signal the sync loop to exit. Used by tests and shutdown paths."""
    _stop_event.set()


def now() -> datetime:
    """Return tz-aware authoritative-time datetime.

    NTP-corrected if a sync has succeeded, otherwise the system clock
    in the resolved tz. Falls back to UTC if no tz can be resolved.
    """
    with _lock:
        offset = _state.offset_seconds if _state.synced else 0.0
    tz = _resolve_tz()
    return datetime.fromtimestamp(_time.time() + offset, tz=tz)


def as_prompt() -> str:
    """One-line system-message string for context injection.

    Format: ``Current time: Saturday 2026-05-02 13:03``.
    """
    return now().strftime("Current time: %A %Y-%m-%d %H:%M")


def status() -> dict[str, Any]:
    """Operator-facing sync status. Used by the System page card."""
    with _lock:
        st = _state
        snapshot = {
            "enabled": _config.enabled if _config else False,
            "synced": st.synced,
            "last_sync_at": st.last_sync_at,
            "last_sync_server": st.last_sync_server,
            "offset_seconds": st.offset_seconds,
            "timezone": _resolve_tz_name(),
        }
    return snapshot


def reset_for_tests() -> None:
    """Clear all module state. Test-only — production code never calls
    this."""
    global _config, _thread, _state
    _stop_event.set()
    if _thread is not None and _thread.is_alive():
        _thread.join(timeout=1.0)
    _thread = None
    _stop_event.clear()
    _state = _SyncState()
    _config = None


# ── internals ──────────────────────────────────────────────────────


def _resolve_tz():
    if ZoneInfo is None:  # pragma: no cover — zoneinfo missing
        return None
    name = _resolve_tz_name()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("time_source: unknown timezone {!r}, falling back to UTC", name)
        return ZoneInfo("UTC")


def _resolve_tz_name() -> str:
    if _config is None:
        return "UTC"
    if _config.timezone_source == "manual" and _config.timezone_manual:
        return _config.timezone_manual
    if _config.weather_cache_getter is not None:
        try:
            data = _config.weather_cache_getter()
        except Exception as exc:
            logger.warning(
                "time_source: weather_cache_getter raised {}; falling back to UTC", exc
            )
            return "UTC"
        if isinstance(data, dict):
            tz = data.get("timezone")
            if isinstance(tz, str) and tz:
                return tz
    return "UTC"


def _sync_loop() -> None:
    while not _stop_event.is_set():
        _refresh_offset()
        if _config is None:
            return
        if _stop_event.wait(_config.refresh_interval_s):
            return


def _refresh_offset() -> bool:
    """Try each configured NTP server in order until one responds.

    Returns True on success. On total failure, logs a warning and
    leaves prior offset state untouched — operators still get a
    timestamp from the system clock until the next sync attempt
    succeeds.
    """
    if _config is None or not _config.ntp_servers:
        return False
    try:
        import ntplib
    except ImportError:
        logger.warning(
            "time_source: ntplib not installed; system clock will be used unsynchronized"
        )
        return False
    client = ntplib.NTPClient()
    for server in _config.ntp_servers:
        try:
            resp = client.request(server, version=3, timeout=5)
        except Exception as exc:
            logger.debug(
                "time_source: NTP server {!r} did not respond ({})", server, exc
            )
            continue
        with _lock:
            _state.offset_seconds = float(resp.offset)
            _state.last_sync_at = _time.time()
            _state.last_sync_server = server
            _state.synced = True
        logger.success(
            "time_source: NTP sync via {} (offset {:+.3f} s)", server, resp.offset
        )
        return True
    logger.warning(
        "time_source: all NTP servers ({}) unreachable; using system clock",
        ", ".join(_config.ntp_servers),
    )
    return False
