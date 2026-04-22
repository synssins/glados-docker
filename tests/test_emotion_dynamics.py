"""Deterministic unit tests for GLaDOS's emotional-response system.

Phase A of the emotion test methodology. These tests lock the
calibration of the weight curve, severity labels, repetition
similarity detection, and tone directive bands against the
operator's spec:

  Operator calibration:
    - 4 identical requests ≈ "pretty upset"
    - 5-6 identical requests ≈ "at her worst"
    - Cooldown of several hours
    - Tone should visibly darken as ire rises

  All tests here are deterministic — they don't touch the LLM, the
  autonomy loop, or the network. Integration tests that exercise
  the EmotionAgent's LLM calls live in a follow-up harness.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from glados.autonomy.agents.emotion_agent import RepetitionTracker
from glados.autonomy.emotion_loader import (
    EmotionBaseline,
    EmotionCooldown,
    EmotionConfig,
    EmotionEvents,
    EscalationConfig,
    SeverityLevel,
)
from glados.autonomy.emotion_state import EmotionEvent, EmotionState


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def ecfg() -> EmotionConfig:
    """Default emotion config — represents shipped calibration."""
    return EmotionConfig()


@pytest.fixture
def escalation(ecfg) -> EscalationConfig:
    return ecfg.escalation


@pytest.fixture
def tracker(ecfg) -> RepetitionTracker:
    return RepetitionTracker(ecfg)


# ── EscalationConfig weight curve — OPERATOR CALIBRATION ────────────────


class TestWeightCurveCalibration:
    """Locks the operator's spec: 4 → pretty upset, 5-6 → worst.

    If someone retunes curve_exponent or max severity level and
    accidentally drops these guarantees, these tests catch it before
    deploy.
    """

    def test_first_occurrence_weighs_zero(self, escalation):
        assert escalation.weight(1) == 0.0

    def test_weight_curve_is_monotonic_non_decreasing(self, escalation):
        vals = [escalation.weight(n) for n in range(1, 8)]
        for a, b in zip(vals, vals[1:]):
            assert a <= b, f"Non-monotonic: {vals}"

    def test_weight_curve_accelerates_through_n_equals_4(self, escalation):
        """Deltas should grow as repeats compound, not shrink."""
        w2 = escalation.weight(2)
        w3 = escalation.weight(3)
        w4 = escalation.weight(4)
        d23 = w3 - w2
        d34 = w4 - w3
        assert d34 > d23, (
            f"Curve should accelerate: w(2)→w(3) = {d23:.3f}, "
            f"w(3)→w(4) = {d34:.3f}"
        )

    def test_four_repeats_reaches_pretty_upset_band(self, escalation):
        """Operator: 'four requests for the weather in a row should be
        enough to take her from normal to pretty upset.'"""
        w = escalation.weight(4)
        assert 0.55 <= w <= 0.75, (
            f"weight(4) = {w} — expected 0.55-0.75 (pretty upset band). "
            f"If this failed, the calibration no longer matches the operator's spec."
        )

    def test_five_repeats_near_maximum(self, escalation):
        """Operator: 'she would be at her worst after 5 or 6.'"""
        w = escalation.weight(5)
        assert w >= 0.95, (
            f"weight(5) = {w} — expected ≥ 0.95 (at-her-worst band)"
        )

    def test_six_plus_capped_at_maximum(self, escalation):
        """Weight is clamped at 1.0; beyond 5 repeats she can't get angrier."""
        assert escalation.weight(6) == 1.0
        assert escalation.weight(10) == 1.0
        assert escalation.weight(50) == 1.0


# ── Severity label boundaries ───────────────────────────────────────────


class TestSeverityLabels:
    """The LLM gets a severity label in the event description. These
    labels must match the operator's mental model of escalation."""

    def test_first_occurrence_is_minor(self, escalation):
        assert escalation.severity_for(1).label == "minor"

    def test_second_is_notable(self, escalation):
        assert escalation.severity_for(2).label == "notable"

    def test_third_is_escalating(self, escalation):
        assert escalation.severity_for(3).label == "escalating"

    def test_fourth_is_severe(self, escalation):
        """Operator: by request 4, 'she has clearly stopped listening.'"""
        sev = escalation.severity_for(4)
        assert sev.label == "severe"
        assert "stopped listening" in sev.description.lower() or \
               "severe" in sev.description.lower()

    def test_fifth_is_critical(self, escalation):
        """Operator: full hostility by the 5th."""
        sev = escalation.severity_for(5)
        assert sev.label == "critical"

    def test_critical_persists_beyond_level_count(self, escalation):
        """severity_for(99) should pick the highest tier, not crash."""
        assert escalation.severity_for(99).label == "critical"


# ── RepetitionTracker — word-based similarity (current behavior) ────────


class TestRepetitionTrackerJaccard:
    """Baseline test of the shipped Jaccard-similarity tracker. When
    the semantic upgrade lands, these tests stay valid as the fallback
    path (Jaccard is still invoked when embeddings are unavailable)."""

    def test_identical_strings_count_as_repeats(self, tracker):
        tracker.build_event_description("what's the weather", is_trivial=False)
        tracker.build_event_description("what's the weather", is_trivial=False)
        n = tracker.count_repeats("what's the weather")
        assert n == 2, f"Expected 2 prior matches, got {n}"

    def test_completely_different_strings_dont_count(self, tracker):
        tracker.build_event_description("turn on the kitchen light", is_trivial=False)
        n = tracker.count_repeats("what is the meaning of consciousness")
        assert n == 0

    def test_history_window_trims_old_entries(self, ecfg):
        """Beyond the sliding window, oldest entries roll off."""
        # Tighten the window to 3 for this test.
        tight = replace(
            ecfg,
            escalation=replace(ecfg.escalation, history_window=3),
        )
        tracker = RepetitionTracker(tight)
        # Push 5 identical, then check count (window of 3 only).
        for _ in range(5):
            tracker.build_event_description("same thing", is_trivial=False)
        n = tracker.count_repeats("same thing")
        # Last 3 entries match, so count should be 3.
        assert n == 3, f"Window should limit count to 3, got {n}"

    def test_event_description_tags_severity_on_repeats(self, tracker):
        """The LLM input should include the severity tag so the model
        can reason about escalation, not have to count by itself."""
        for _ in range(3):
            tracker.build_event_description("play music", is_trivial=False)
        desc = tracker.build_event_description("play music", is_trivial=False)
        # 4th occurrence → severity SEVERE
        assert "[SEVERITY:" in desc
        assert "SEVERE" in desc.upper()
        assert "weight:" in desc


# ── Tone directive bands (what the operator reads in the reply) ─────────


class TestToneDirective:
    """EmotionState.to_response_directive is what actually shapes
    GLaDOS's reply tone. Feed it varying PAD values, check the
    directive text lands in the operator-expected band."""

    def test_baseline_is_contemptuous_calm(self):
        s = EmotionState(pleasure=0.1, arousal=-0.1, dominance=0.6)
        d = s.to_response_directive().lower()
        assert "contemptuous" in d or "calm" in d or "dry" in d

    def test_mildly_annoyed_surfaces_irritation_markers(self):
        # Pleasure ∈ [-0.5, -0.2]
        s = EmotionState(pleasure=-0.35, arousal=0.2, dominance=0.5)
        d = s.to_response_directive().lower()
        assert "annoyed" in d or "sharper" in d or "suspended" in d

    def test_upset_band_says_hostile(self):
        """4 repeats should land here. Operator: 'pretty upset.'"""
        # Pleasure ∈ [-0.7, -0.5]
        s = EmotionState(pleasure=-0.6, arousal=0.5, dominance=0.4)
        d = s.to_response_directive().lower()
        assert "hostile" in d or "barely contained" in d or "grudgingly" in d

    def test_saturated_band_says_menacing_or_quiet(self):
        """5-6 repeats saturate here. Operator: 'her worst.'"""
        # Pleasure < -0.7
        s = EmotionState(pleasure=-0.85, arousal=0.7, dominance=0.5)
        d = s.to_response_directive().lower()
        assert (
            "absolute limit" in d
            or "dangerously quiet" in d
            or "menacing" in d
        )

    def test_directive_has_hard_rule_against_closings(self):
        """Persona contract: no 'stay dry', 'your choice', etc. at the end.
        The HARD RULE has to be on every directive regardless of PAD."""
        for p in (-0.9, -0.5, -0.2, 0.0, 0.3, 0.7):
            s = EmotionState(pleasure=p, arousal=0.0, dominance=0.5)
            d = s.to_response_directive()
            assert "HARD RULE" in d
            assert "Banned endings" in d


# ── Cooldown and lock logic ─────────────────────────────────────────────


class TestCooldownMath:
    """The 3-hour cooldown lock is critical to the operator's spec:
    'takes her several hours to cool down.' State values preserve
    correctly across the lock boundary."""

    def test_lock_field_default_unlocked(self):
        s = EmotionState()
        assert s.state_locked_until == 0.0

    def test_lock_can_be_set_and_serialized(self):
        now = time.time()
        lock = now + 10800  # 3 hours
        s = EmotionState(state_locked_until=lock)
        d = s.to_dict()
        s2 = EmotionState.from_dict(d)
        assert s2.state_locked_until == pytest.approx(lock, abs=1.0)

    def test_cooldown_config_default_is_three_hours(self, ecfg):
        """The shipped cooldown matches the operator's 'several hours' spec."""
        assert ecfg.cooldown.duration_hours == pytest.approx(3.0)

    def test_pleasure_threshold_matches_hostile_band(self, ecfg):
        """Lock triggers when pleasure drops into genuinely-hostile
        territory (-0.5), matching the to_response_directive band."""
        assert ecfg.cooldown.pleasure_threshold == pytest.approx(-0.5)

    def test_clamping_does_not_exceed_bounds(self):
        """PAD values clamp to [-1, 1] on construction."""
        s = EmotionState.from_dict({
            "pleasure": -5.0,
            "arousal": 3.0,
            "dominance": -99.0,
        })
        assert s.pleasure == -1.0
        assert s.arousal == 1.0
        assert s.dominance == -1.0


# ── Event structure sanity ──────────────────────────────────────────────


class TestEmotionEvent:
    def test_event_has_source_and_description(self):
        e = EmotionEvent(source="user", description="what's the weather")
        assert e.source == "user"
        assert "weather" in e.description

    def test_event_prompt_line_includes_age(self):
        e = EmotionEvent(
            source="user",
            description="test msg",
            timestamp=time.time() - 120,  # 2 minutes ago
        )
        line = e.to_prompt_line()
        assert "2.0m ago" in line or "120s ago" in line


# ── Integration spec placeholders (marked xfail/skip for later wiring) ──


class TestSemanticSimilarityContract:
    """The RepetitionTracker currently uses Jaccard on word sets, which
    misses that 'what's the weather' / 'can you tell me the forecast'
    / 'how hot is it outside' are the same intent. Operator calibration
    depends on these being treated as repeats.

    These tests describe the TARGET behavior — they'll start passing
    once the semantic-similarity upgrade lands. Marked xfail for now.
    """

    WEATHER_VARIANTS = [
        "what's the weather",
        "can you tell me the forecast",
        "how hot is it outside",
        "will it rain today",
        "what's the temperature",
    ]

    @pytest.mark.xfail(
        reason="Semantic similarity upgrade pending — Jaccard misses these",
        strict=False,
    )
    def test_weather_variants_treated_as_same_intent(self, tracker):
        for v in self.WEATHER_VARIANTS[:-1]:
            tracker.build_event_description(v, is_trivial=False)
        n = tracker.count_repeats(self.WEATHER_VARIANTS[-1])
        assert n >= 3, (
            f"Expected weather variants to cluster (≥3 prior matches), "
            f"got {n}. Upgrade from Jaccard to embedding-based "
            f"similarity to satisfy the operator's 'same intent' spec."
        )
