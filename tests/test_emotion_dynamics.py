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

from glados.autonomy.agents.emotion_agent import (
    RepetitionTracker,
    make_embedding_similarity,
)
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


class TestSemanticSimilarityInjection:
    """Phase Emotion-B (2026-04-22): RepetitionTracker accepts a
    pluggable similar_fn so the operator's 'what's the weather' /
    'can you tell me the forecast' / 'how hot is it outside' variants
    cluster as the same intent. These tests verify the INJECTION
    mechanism using a mock; a separate class tests against real BGE
    embeddings when the model is available."""

    WEATHER_VARIANTS = [
        "what's the weather",
        "can you tell me the forecast",
        "how hot is it outside",
        "will it rain today",
        "what's the temperature",
    ]

    @staticmethod
    def _weather_intent_similar(a: str, b: str) -> bool:
        """Tiny keyword-cluster mock used to test the injection
        mechanism without loading a model. Real production path uses
        make_embedding_similarity(BGE) instead."""
        keywords = {"weather", "forecast", "hot", "cold", "rain", "temperature"}
        def has_weather(s: str) -> bool:
            return any(k in s.lower() for k in keywords)
        return has_weather(a) and has_weather(b)

    def test_injected_similar_fn_is_used_for_counting(self, ecfg):
        tracker = RepetitionTracker(ecfg, similar_fn=self._weather_intent_similar)
        for v in self.WEATHER_VARIANTS[:-1]:
            tracker.build_event_description(v, is_trivial=False)
        n = tracker.count_repeats(self.WEATHER_VARIANTS[-1])
        assert n >= 3, (
            f"Expected weather variants to cluster (≥3 prior matches) "
            f"via injected similar_fn; got {n}. Injection is broken."
        )

    def test_weather_variants_escalate_severity_via_injection(self, ecfg):
        tracker = RepetitionTracker(ecfg, similar_fn=self._weather_intent_similar)
        # Push 3 weather-variant messages.
        for v in self.WEATHER_VARIANTS[:3]:
            tracker.build_event_description(v, is_trivial=False)
        # The 4th occurrence should produce a SEVERE-tagged description
        # (weight(4) ≈ 0.65, 'pretty upset' band).
        desc = tracker.build_event_description(self.WEATHER_VARIANTS[3], is_trivial=False)
        assert "SEVERE" in desc.upper(), (
            f"Expected 4th weather variant to be tagged SEVERE; got: {desc!r}"
        )

    def test_default_behavior_unchanged_when_no_fn_injected(self, ecfg):
        """Back-compat: no similar_fn → Jaccard path exactly as before."""
        tracker = RepetitionTracker(ecfg)  # no similar_fn
        # These differ enough that Jaccard should NOT cluster them.
        tracker.build_event_description("what's the weather", is_trivial=False)
        n = tracker.count_repeats("how hot is it outside")
        assert n == 0, (
            "Default Jaccard path should not cluster weather paraphrases. "
            "If this fails, the default path regressed."
        )

    def test_identity_short_circuit(self, ecfg):
        """A similar_fn that always returns False should still count
        exact-identity matches via the '== True' short-circuit? No:
        our contract says similar_fn is authoritative. Double-check
        the contract: identical strings should count as repeats only
        if the similarity function says so."""
        tracker = RepetitionTracker(ecfg, similar_fn=lambda a, b: False)
        tracker.build_event_description("exact", is_trivial=False)
        n = tracker.count_repeats("exact")
        # This is actually the contract — the injected function wins.
        # If an operator injects a broken similar_fn, they own the
        # outcome; we don't silently override.
        assert n == 0


class TestEmbeddingSimilarityFactory:
    """Tests for make_embedding_similarity() using a mock embedder
    so we can assert the predicate logic without loading a model."""

    class MockEmbedder:
        """Returns fixed pseudo-embeddings per keyword. Two vectors are
        highly similar if their strings share any keyword."""

        def __init__(self):
            import numpy as np
            # Simple per-keyword one-hot embeddings in 3D.
            self._keywords = {
                "weather": np.array([1.0, 0.0, 0.0]),
                "forecast": np.array([0.95, 0.1, 0.0]),   # near weather
                "hot": np.array([0.9, 0.15, 0.0]),        # near weather
                "lights": np.array([0.0, 1.0, 0.0]),
                "music": np.array([0.0, 0.0, 1.0]),
            }
            self._np = np

        def embed(self, texts, is_query=False):
            vecs = []
            for t in texts:
                low = t.lower()
                # Sum vectors for each keyword found.
                v = self._np.zeros(3, dtype=float)
                hits = 0
                for k, kv in self._keywords.items():
                    if k in low:
                        v += kv
                        hits += 1
                if hits == 0:
                    v = self._np.array([0.1, 0.1, 0.1])  # neutral
                # L2 normalize.
                n = self._np.linalg.norm(v)
                if n > 0:
                    v = v / n
                vecs.append(v)
            return self._np.array(vecs)

    def test_identical_strings_similar(self):
        fn = make_embedding_similarity(self.MockEmbedder(), threshold=0.70)
        assert fn("what's the weather", "what's the weather") is True

    def test_paraphrase_pair_similar(self):
        fn = make_embedding_similarity(self.MockEmbedder(), threshold=0.70)
        # Both contain "weather" / "forecast" keywords → near-parallel vecs.
        assert fn("what's the weather", "can you tell me the forecast") is True

    def test_unrelated_pair_not_similar(self):
        fn = make_embedding_similarity(self.MockEmbedder(), threshold=0.70)
        assert fn("what's the weather", "turn on the lights") is False

    def test_caches_embeddings_across_calls(self):
        mock = self.MockEmbedder()
        calls = {"n": 0}
        original_embed = mock.embed
        def counting_embed(texts, is_query=False):
            calls["n"] += 1
            return original_embed(texts, is_query=is_query)
        mock.embed = counting_embed
        fn = make_embedding_similarity(mock, threshold=0.70)
        fn("what's the weather", "forecast please")
        n1 = calls["n"]
        # Second call with same strings — should hit cache.
        fn("what's the weather", "forecast please")
        n2 = calls["n"]
        assert n2 == n1, (
            f"Expected embeddings to be cached across repeat calls; "
            f"got {n2 - n1} additional embed() calls."
        )


class TestSemanticWithRealBGE:
    """End-to-end check with the actual BGE-small ONNX model. Skipped
    automatically if the model isn't present in this environment (CI
    without ML assets, dev machines without the bundle, etc.)."""

    @pytest.fixture(scope="class")
    def embedder(self):
        try:
            from glados.ha.semantic_index import Embedder
            return Embedder()
        except Exception as e:
            pytest.skip(f"BGE model unavailable: {e}")

    def test_weather_variants_cluster_under_bge(self, embedder, ecfg):
        """Operator's spec-level truth: real BGE should cluster the
        canonical weather paraphrases. Threshold tuned for this."""
        similar_fn = make_embedding_similarity(embedder, threshold=0.70)
        tracker = RepetitionTracker(ecfg, similar_fn=similar_fn)

        variants = [
            "what's the weather",
            "can you tell me the forecast",
            "how hot is it outside",
            "will it rain today",
        ]
        for v in variants[:-1]:
            tracker.build_event_description(v, is_trivial=False)

        n = tracker.count_repeats(variants[-1])
        assert n >= 2, (
            f"Real BGE clustered only {n}/3 of the canonical weather "
            f"paraphrases. Tune the threshold or verify the model "
            f"bundle is correct."
        )

    def test_unrelated_commands_stay_separate_under_bge(self, embedder, ecfg):
        """Complement of the above — semantically distinct requests
        shouldn't falsely cluster just because BGE is generous."""
        similar_fn = make_embedding_similarity(embedder, threshold=0.70)
        tracker = RepetitionTracker(ecfg, similar_fn=similar_fn)
        tracker.build_event_description("what's the weather", is_trivial=False)
        tracker.build_event_description("turn on the kitchen light", is_trivial=False)
        tracker.build_event_description("play some jazz", is_trivial=False)
        n = tracker.count_repeats("unlock the front door")
        assert n == 0, (
            f"Real BGE falsely clustered unrelated commands ({n} matches). "
            f"Raise the threshold or audit the test strings."
        )
