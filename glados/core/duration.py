"""Parse human durations like '30d' or 'never' into seconds."""
from __future__ import annotations

import re

NEVER = None

_PATTERN = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.IGNORECASE)
_MULTIPLIER = {
    "": 1,
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}


def parse_duration(value: str) -> int | None:
    """Parse '30d' / 'never' / bare integer seconds into seconds.

    Returns NEVER (None) for 'never'. Raises ValueError on any other
    unparseable input.
    """
    if not isinstance(value, str):
        raise ValueError(f"duration must be str, got {type(value).__name__}")
    s = value.strip()
    if s.lower() == "never":
        return NEVER
    m = _PATTERN.match(s)
    if not m:
        raise ValueError(f"cannot parse duration: {value!r}")
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n * _MULTIPLIER[unit]
