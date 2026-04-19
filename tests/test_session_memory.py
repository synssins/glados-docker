"""Tests for glados.core.session_memory."""

from __future__ import annotations

import pytest

from glados.core.session_memory import (
    DEFAULT_BUFFER_SIZE,
    SessionMemory,
    Turn,
)


class _FakeClock:
    """Monotonic manual clock for deterministic TTL tests."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _turn(t: float = 0.0, utt: str = "turn on the lights", area: str | None = "office",
          entities: tuple[str, ...] = ("light.office_lamp",),
          verb: str = "turn_on") -> Turn:
    return Turn(
        timestamp=t,
        utterance=utt,
        resolved_area_id=area,
        entities_affected=entities,
        action_verb=verb,
    )


class TestConstruction:
    def test_rejects_zero_buffer(self) -> None:
        with pytest.raises(ValueError):
            SessionMemory(buffer_size=0)

    def test_rejects_nonpositive_ttl(self) -> None:
        with pytest.raises(ValueError):
            SessionMemory(idle_ttl_seconds=0)
        with pytest.raises(ValueError):
            SessionMemory(idle_ttl_seconds=-1)


class TestRecordAndRead:
    def test_empty_session_returns_nothing(self) -> None:
        mem = SessionMemory()
        assert mem.recent_turns("s1") == []
        assert mem.last_turn("s1") is None

    def test_empty_session_id_rejected(self) -> None:
        mem = SessionMemory()
        with pytest.raises(ValueError):
            mem.record_turn("", _turn())

    def test_single_turn_roundtrip(self) -> None:
        mem = SessionMemory()
        t = _turn()
        mem.record_turn("s1", t)
        assert mem.recent_turns("s1") == [t]
        assert mem.last_turn("s1") == t

    def test_ring_buffer_caps_at_buffer_size(self) -> None:
        mem = SessionMemory(buffer_size=3)
        for i in range(5):
            mem.record_turn("s1", _turn(t=float(i), utt=f"u{i}"))
        turns = mem.recent_turns("s1")
        assert len(turns) == 3
        # Oldest two dropped; the retained turns are the three most recent
        assert [t.utterance for t in turns] == ["u2", "u3", "u4"]

    def test_default_buffer_size(self) -> None:
        mem = SessionMemory()
        for i in range(DEFAULT_BUFFER_SIZE + 2):
            mem.record_turn("s1", _turn(t=float(i), utt=f"u{i}"))
        assert len(mem.recent_turns("s1")) == DEFAULT_BUFFER_SIZE

    def test_recent_turns_limit(self) -> None:
        mem = SessionMemory()
        for i in range(5):
            mem.record_turn("s1", _turn(t=float(i), utt=f"u{i}"))
        assert [t.utterance for t in mem.recent_turns("s1", limit=2)] == ["u3", "u4"]
        assert [t.utterance for t in mem.recent_turns("s1", limit=0)] == []


class TestIdleTtl:
    def test_session_expires_after_idle(self) -> None:
        clock = _FakeClock()
        mem = SessionMemory(idle_ttl_seconds=10.0, now_fn=clock)
        mem.record_turn("s1", _turn())
        assert mem.last_turn("s1") is not None

        clock.advance(11.0)
        assert mem.last_turn("s1") is None
        assert mem.recent_turns("s1") == []

    def test_activity_refreshes_ttl(self) -> None:
        # Continued activity keeps the session alive — otherwise a
        # long conversation at the 10-minute mark would blink out.
        clock = _FakeClock()
        mem = SessionMemory(idle_ttl_seconds=10.0, now_fn=clock)
        mem.record_turn("s1", _turn())

        for _ in range(3):
            clock.advance(9.0)  # below TTL each time
            mem.record_turn("s1", _turn(t=clock.t))
        # Total elapsed time: 27s, but never idle for >9s.
        assert mem.last_turn("s1") is not None

    def test_read_also_refreshes_ttl(self) -> None:
        clock = _FakeClock()
        mem = SessionMemory(idle_ttl_seconds=10.0, now_fn=clock)
        mem.record_turn("s1", _turn())

        clock.advance(9.0)
        # A read counts as activity — the user is actively engaged.
        assert mem.last_turn("s1") is not None
        clock.advance(9.0)
        assert mem.last_turn("s1") is not None  # still alive

    def test_gc_runs_opportunistically(self) -> None:
        clock = _FakeClock()
        mem = SessionMemory(idle_ttl_seconds=5.0, now_fn=clock)
        mem.record_turn("s1", _turn())
        mem.record_turn("s2", _turn())
        clock.advance(6.0)
        # Writing to s3 triggers GC sweep on s1 + s2
        mem.record_turn("s3", _turn())
        assert mem.active_session_count() == 1
        assert mem.last_turn("s1") is None
        assert mem.last_turn("s2") is None


class TestAdmin:
    def test_forget_removes_session(self) -> None:
        mem = SessionMemory()
        mem.record_turn("s1", _turn())
        mem.forget("s1")
        assert mem.last_turn("s1") is None

    def test_forget_unknown_session_is_noop(self) -> None:
        mem = SessionMemory()
        mem.forget("never_existed")  # must not raise

    def test_clear_removes_all(self) -> None:
        mem = SessionMemory()
        for i in range(5):
            mem.record_turn(f"s{i}", _turn())
        mem.clear()
        assert mem.active_session_count() == 0

    def test_active_session_count_reflects_activity(self) -> None:
        clock = _FakeClock()
        mem = SessionMemory(idle_ttl_seconds=10.0, now_fn=clock)
        assert mem.active_session_count() == 0
        mem.record_turn("s1", _turn())
        mem.record_turn("s2", _turn())
        assert mem.active_session_count() == 2
        clock.advance(11.0)
        assert mem.active_session_count() == 0


class TestTurnData:
    def test_turn_is_frozen(self) -> None:
        t = _turn()
        with pytest.raises((AttributeError, TypeError)):
            t.utterance = "mutated"  # type: ignore[misc]

    def test_turn_preserves_all_fields(self) -> None:
        t = Turn(
            timestamp=123.4,
            utterance="set the lights to 50%",
            resolved_area_id="office",
            entities_affected=("light.office_lamp", "light.office_desk"),
            action_verb="brightness_set",
            service="light.turn_on",
            service_data={"brightness_pct": 50},
        )
        mem = SessionMemory()
        mem.record_turn("s1", t)
        got = mem.last_turn("s1")
        assert got == t
        assert got is not None
        assert got.service_data == {"brightness_pct": 50}
