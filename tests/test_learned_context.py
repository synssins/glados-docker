"""Tests for glados.core.learned_context."""

from __future__ import annotations

from pathlib import Path

import pytest

from glados.core.learned_context import (
    LearnedContextStore,
    normalize_utterance,
)


class _FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance_days(self, days: float) -> None:
        self.t += days * 86_400.0


@pytest.fixture
def store(tmp_path: Path) -> LearnedContextStore:
    s = LearnedContextStore(tmp_path / "learned.db")
    yield s
    s.close()


class TestNormalization:
    def test_basic_lowercasing(self) -> None:
        assert normalize_utterance("Turn on the Lights") == "turn on the lights"

    def test_strips_punctuation_except_apostrophes(self) -> None:
        assert normalize_utterance("It's too bright!") == "it's too bright"
        assert normalize_utterance("Turn on the lights, please.") == "turn on the lights please"

    def test_collapses_whitespace(self) -> None:
        assert normalize_utterance("turn    on\tthe  lights") == "turn on the lights"

    def test_empty_input(self) -> None:
        assert normalize_utterance("") == ""
        assert normalize_utterance("   ") == ""

    def test_distinct_commands_stay_distinct(self) -> None:
        # Stopword stripping would collapse these. We deliberately
        # don't strip, so "brighter" and "dimmer" are different keys.
        assert normalize_utterance("brighter") != normalize_utterance("dimmer")
        assert normalize_utterance("turn on the lights") != normalize_utterance("turn off the lights")


class TestSchema:
    def test_fresh_db_created(self, tmp_path: Path) -> None:
        db = LearnedContextStore(tmp_path / "l.db")
        try:
            assert (tmp_path / "l.db").exists()
            assert db.count() == 0
        finally:
            db.close()

    def test_reopen_idempotent(self, tmp_path: Path) -> None:
        LearnedContextStore(tmp_path / "l.db").close()
        db = LearnedContextStore(tmp_path / "l.db")
        try:
            assert db.count() == 0
        finally:
            db.close()

    def test_rejects_nonpositive_ttl(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            LearnedContextStore(tmp_path / "l.db", idle_ttl_days=0)


class TestRecordSuccess:
    def test_first_record_creates_row(self, store: LearnedContextStore) -> None:
        row = store.record_success(
            utterance="brighter",
            source_channel="chat",
            source_area_id=None,
            resolved_area_id="office",
            resolved_verb="brightness_up",
            resolved_tier="lamp",
        )
        assert row.reinforcement == 1
        assert row.resolved_area_id == "office"
        assert row.utterance_key == "brighter"
        assert store.count() == 1

    def test_duplicate_resolution_bumps_reinforcement(self, store: LearnedContextStore) -> None:
        for _ in range(3):
            row = store.record_success(
                utterance="Brighter",
                source_channel="chat",
                source_area_id=None,
                resolved_area_id="office",
                resolved_verb="brightness_up",
                resolved_tier="lamp",
            )
        # Three identical successes → single row with reinforcement=3
        assert store.count() == 1
        assert row.reinforcement == 3

    def test_different_resolution_creates_new_row(self, store: LearnedContextStore) -> None:
        store.record_success(
            utterance="brighter", source_channel="chat", source_area_id=None,
            resolved_area_id="office", resolved_verb="brightness_up",
            resolved_tier="lamp",
        )
        store.record_success(
            utterance="brighter", source_channel="chat", source_area_id=None,
            resolved_area_id="bedroom", resolved_verb="brightness_up",
            resolved_tier="lamp",
        )
        # Same utterance, different resolved area → two rows
        assert store.count() == 2

    def test_different_channel_creates_new_row(self, store: LearnedContextStore) -> None:
        store.record_success(
            utterance="brighter", source_channel="chat", source_area_id=None,
            resolved_area_id="office", resolved_verb="brightness_up",
            resolved_tier="lamp",
        )
        store.record_success(
            utterance="brighter", source_channel="voice", source_area_id=None,
            resolved_area_id="office", resolved_verb="brightness_up",
            resolved_tier="lamp",
        )
        assert store.count() == 2

    def test_source_area_null_vs_value_are_distinct(self, store: LearnedContextStore) -> None:
        # "brighter" spoken with an area header attached is a
        # different case from "brighter" with no area attached.
        # They should not collapse into one learned row.
        store.record_success(
            utterance="brighter", source_channel="voice", source_area_id=None,
            resolved_area_id="office", resolved_verb="brightness_up",
            resolved_tier="lamp",
        )
        store.record_success(
            utterance="brighter", source_channel="voice", source_area_id="office",
            resolved_area_id="office", resolved_verb="brightness_up",
            resolved_tier="lamp",
        )
        assert store.count() == 2

    def test_empty_utterance_rejected(self, store: LearnedContextStore) -> None:
        with pytest.raises(ValueError):
            store.record_success(
                utterance="   ", source_channel="chat", source_area_id=None,
                resolved_area_id="office", resolved_verb="turn_on",
            )


class TestLookup:
    def test_miss_returns_empty(self, store: LearnedContextStore) -> None:
        assert store.lookup(
            utterance="brighter", source_channel="chat", source_area_id=None,
        ) == []

    def test_hit_returns_matching_row(self, store: LearnedContextStore) -> None:
        store.record_success(
            utterance="brighter", source_channel="chat", source_area_id=None,
            resolved_area_id="office", resolved_verb="brightness_up",
            resolved_tier="lamp",
        )
        rows = store.lookup(
            utterance="brighter", source_channel="chat", source_area_id=None,
        )
        assert len(rows) == 1
        assert rows[0].resolved_area_id == "office"

    def test_ranks_by_reinforcement(self, store: LearnedContextStore) -> None:
        # Office wins: resolved there 3x, bedroom 1x
        for _ in range(3):
            store.record_success(
                utterance="brighter", source_channel="chat", source_area_id=None,
                resolved_area_id="office", resolved_verb="brightness_up",
                resolved_tier="lamp",
            )
        store.record_success(
            utterance="brighter", source_channel="chat", source_area_id=None,
            resolved_area_id="bedroom", resolved_verb="brightness_up",
            resolved_tier="lamp",
        )
        rows = store.lookup(
            utterance="brighter", source_channel="chat", source_area_id=None,
        )
        assert len(rows) == 2
        assert rows[0].resolved_area_id == "office"
        assert rows[0].reinforcement == 3
        assert rows[1].resolved_area_id == "bedroom"

    def test_respects_channel_filter(self, store: LearnedContextStore) -> None:
        store.record_success(
            utterance="brighter", source_channel="chat", source_area_id=None,
            resolved_area_id="office", resolved_verb="brightness_up",
            resolved_tier="lamp",
        )
        # Voice channel lookup should not see chat's learned row
        assert store.lookup(
            utterance="brighter", source_channel="voice", source_area_id=None,
        ) == []

    def test_empty_utterance_returns_empty(self, store: LearnedContextStore) -> None:
        assert store.lookup(utterance="", source_channel="chat", source_area_id=None) == []

    def test_punctuation_does_not_affect_match(self, store: LearnedContextStore) -> None:
        # Learned under one form, looked up under another.
        store.record_success(
            utterance="Brighter!",
            source_channel="chat", source_area_id=None,
            resolved_area_id="office", resolved_verb="brightness_up",
        )
        rows = store.lookup(
            utterance="brighter", source_channel="chat", source_area_id=None,
        )
        assert len(rows) == 1

    def test_lookup_does_not_touch_last_used(
        self, store: LearnedContextStore, tmp_path: Path,
    ) -> None:
        # A lookup isn't a success yet — only record_success advances
        # last_used_at. Otherwise a stale row that keeps getting
        # looked up but never validated would refresh its own TTL.
        clock = _FakeClock()
        store.close()
        s = LearnedContextStore(tmp_path / "l.db", now_fn=clock)
        try:
            s.record_success(
                utterance="brighter", source_channel="chat",
                source_area_id=None, resolved_area_id="office",
                resolved_verb="brightness_up",
            )
            original_lu = s.lookup(
                utterance="brighter", source_channel="chat", source_area_id=None,
            )[0].last_used_at
            clock.advance_days(1)
            again = s.lookup(
                utterance="brighter", source_channel="chat", source_area_id=None,
            )[0].last_used_at
            assert again == original_lu
        finally:
            s.close()


class TestBumpFailure:
    def test_decrement_keeps_row_when_positive(self, store: LearnedContextStore) -> None:
        # Two wins, then one failure — net reinforcement = 1, row lives.
        row = store.record_success(
            utterance="brighter", source_channel="chat", source_area_id=None,
            resolved_area_id="office", resolved_verb="brightness_up",
        )
        store.record_success(
            utterance="brighter", source_channel="chat", source_area_id=None,
            resolved_area_id="office", resolved_verb="brightness_up",
        )
        updated = store.bump_failure(row.id)
        assert updated is not None
        assert updated.reinforcement == 1
        assert store.count() == 1

    def test_decrement_deletes_on_zero(self, store: LearnedContextStore) -> None:
        # Single win + single failure = reinforcement 0 → delete.
        row = store.record_success(
            utterance="brighter", source_channel="chat", source_area_id=None,
            resolved_area_id="office", resolved_verb="brightness_up",
        )
        result = store.bump_failure(row.id)
        assert result is None
        assert store.count() == 0

    def test_decrement_nonexistent_row_is_noop(self, store: LearnedContextStore) -> None:
        assert store.bump_failure(99999) is None


class TestForget:
    def test_forget_deletes_row(self, store: LearnedContextStore) -> None:
        row = store.record_success(
            utterance="brighter", source_channel="chat", source_area_id=None,
            resolved_area_id="office", resolved_verb="brightness_up",
        )
        store.forget(row.id)
        assert store.count() == 0

    def test_forget_unknown_row_is_noop(self, store: LearnedContextStore) -> None:
        store.forget(99999)  # must not raise


class TestSweep:
    def test_removes_rows_past_ttl(self, tmp_path: Path) -> None:
        clock = _FakeClock()
        s = LearnedContextStore(
            tmp_path / "l.db", idle_ttl_days=14, now_fn=clock,
        )
        try:
            s.record_success(
                utterance="old_pattern", source_channel="chat",
                source_area_id=None, resolved_area_id="office",
                resolved_verb="turn_on",
            )
            clock.advance_days(7)
            s.record_success(
                utterance="recent_pattern", source_channel="chat",
                source_area_id=None, resolved_area_id="office",
                resolved_verb="turn_on",
            )
            clock.advance_days(8)  # old_pattern is now 15 days stale
            deleted = s.sweep()
            assert deleted == 1
            assert s.count() == 1
            remaining = s.lookup(
                utterance="recent_pattern", source_channel="chat", source_area_id=None,
            )
            assert len(remaining) == 1
        finally:
            s.close()

    def test_sweep_with_no_expired_rows(self, store: LearnedContextStore) -> None:
        store.record_success(
            utterance="brighter", source_channel="chat", source_area_id=None,
            resolved_area_id="office", resolved_verb="brightness_up",
        )
        assert store.sweep() == 0
        assert store.count() == 1

    def test_record_success_refreshes_last_used(self, tmp_path: Path) -> None:
        # A row that keeps getting used should never decay — the
        # pattern is active.
        clock = _FakeClock()
        s = LearnedContextStore(
            tmp_path / "l.db", idle_ttl_days=14, now_fn=clock,
        )
        try:
            s.record_success(
                utterance="brighter", source_channel="chat",
                source_area_id=None, resolved_area_id="office",
                resolved_verb="brightness_up",
            )
            clock.advance_days(10)
            s.record_success(
                utterance="brighter", source_channel="chat",
                source_area_id=None, resolved_area_id="office",
                resolved_verb="brightness_up",
            )
            clock.advance_days(10)  # 20 days since initial, 10 since last
            assert s.sweep() == 0
            assert s.count() == 1
        finally:
            s.close()


class TestPersistence:
    def test_reopen_preserves_rows(self, tmp_path: Path) -> None:
        s = LearnedContextStore(tmp_path / "l.db")
        try:
            s.record_success(
                utterance="brighter", source_channel="chat",
                source_area_id=None, resolved_area_id="office",
                resolved_verb="brightness_up",
            )
        finally:
            s.close()
        # Reopen — row should still be there
        s2 = LearnedContextStore(tmp_path / "l.db")
        try:
            assert s2.count() == 1
            rows = s2.lookup(
                utterance="brighter", source_channel="chat", source_area_id=None,
            )
            assert len(rows) == 1
            assert rows[0].resolved_area_id == "office"
        finally:
            s2.close()
