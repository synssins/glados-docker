"""Clock indirection for the emotion subsystem.

Phase Emotion-D (2026-04-22): every time.time() call in the emotion
state / agent / event pipeline routes through emotion_now() so tests
can time-travel without waiting for the 3-hour cooldown decay.

Set GLADOS_EMOTION_CLOCK_OVERRIDE to an epoch-seconds float and the
helper returns that value instead of the wall clock. Unset (or any
parse failure) falls back to time.time() exactly.

Usage in tests:

    import os
    os.environ['GLADOS_EMOTION_CLOCK_OVERRIDE'] = str(time.time() + 10800)
    # ... advance-time assertions ...
    del os.environ['GLADOS_EMOTION_CLOCK_OVERRIDE']

The override is a *point in time*, not a *time delta*, so advancing
requires re-setting the env var. A helper for test fixtures can wrap
this to offer time-travel semantics (see
tests/test_emotion_dynamics.py if that fixture lands).

The override is read on every call — no caching — so test fixtures
can update it mid-run. Production overhead is one os.environ.get()
per emotion tick, which is negligible.
"""

from __future__ import annotations

import os
import time

_OVERRIDE_VAR = "GLADOS_EMOTION_CLOCK_OVERRIDE"


def emotion_now() -> float:
    """Current epoch seconds, respecting GLADOS_EMOTION_CLOCK_OVERRIDE."""
    raw = os.environ.get(_OVERRIDE_VAR)
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return time.time()
