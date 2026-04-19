"""Tests for glados.observability.priority — chat/autonomy coordination.

The gate is a process-wide counter + grace-window timestamp. Chat-path
callers wrap their Ollama round-trip in `chat_in_flight()`; autonomy
callers check `is_chat_in_flight()` and skip the tick when it's True.
"""
from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _reset_gate():
    """Reset the module-level state so tests don't leak into each other."""
    from glados.observability import priority
    with priority._lock:
        priority._active_chats = 0
        priority._last_chat_end_ts = 0.0
    yield
    with priority._lock:
        priority._active_chats = 0
        priority._last_chat_end_ts = 0.0


def test_idle_gate_reports_not_in_flight() -> None:
    from glados.observability import is_chat_in_flight
    assert is_chat_in_flight() is False


def test_active_context_reports_in_flight() -> None:
    from glados.observability import chat_in_flight, is_chat_in_flight
    with chat_in_flight():
        assert is_chat_in_flight() is True
    # After exit, the grace window keeps the gate hot briefly.
    assert is_chat_in_flight() is True


def test_grace_window_expires(monkeypatch) -> None:
    """After the grace window elapses, autonomy is free to resume."""
    from glados.observability import priority
    with priority.chat_in_flight():
        pass
    # Fast-forward the clock past the grace window.
    future = priority._now() + priority._GRACE_AFTER_CHAT_S + 0.1
    monkeypatch.setattr(priority, "_now", lambda: future)
    assert priority.is_chat_in_flight() is False


def test_nested_contexts_keep_gate_held() -> None:
    from glados.observability import chat_in_flight, is_chat_in_flight
    with chat_in_flight():
        with chat_in_flight():
            assert is_chat_in_flight() is True
        # Inner exit must NOT release the gate while outer is still active.
        assert is_chat_in_flight() is True
    # Outer exit releases, grace window starts.
    assert is_chat_in_flight() is True


def test_concurrent_holders_all_release() -> None:
    """Two threads enter, both must exit before the gate clears."""
    from glados.observability import chat_in_flight, is_chat_in_flight

    t1_enter = threading.Event()
    t1_release = threading.Event()

    def worker() -> None:
        with chat_in_flight():
            t1_enter.set()
            t1_release.wait(2.0)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert t1_enter.wait(2.0), "worker didn't enter"
    # Main thread also takes the gate; both holders now.
    with chat_in_flight():
        assert is_chat_in_flight() is True
    # Main released but worker still holds — gate should still report busy.
    assert is_chat_in_flight() is True
    t1_release.set()
    t.join(2.0)


def test_exception_still_releases_gate() -> None:
    from glados.observability import chat_in_flight, priority

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with chat_in_flight():
            raise _Boom()
    # Counter back to 0 even though the body raised.
    with priority._lock:
        assert priority._active_chats == 0


def test_autonomy_loop_skips_when_chat_in_flight() -> None:
    """AutonomyLoop._should_skip must honour the priority gate."""
    from glados.observability import chat_in_flight
    from glados.autonomy.loop import AutonomyLoop

    # Build a stub loop — we only care about _should_skip behavior.
    class _Cfg:
        cooldown_s = 0

    loop = AutonomyLoop.__new__(AutonomyLoop)
    loop._currently_speaking_event = threading.Event()   # clear
    loop._config = _Cfg()
    loop._last_prompt_ts = 0.0

    # Idle: not skipped.
    assert loop._should_skip() is False

    # Chat in flight: skipped.
    with chat_in_flight():
        assert loop._should_skip() is True
    # Post-chat grace window: still skipped.
    assert loop._should_skip() is True
