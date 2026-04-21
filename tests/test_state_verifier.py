"""Tests for glados.ha.state_verifier — Phase 8.4."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import pytest

from glados.ha.state_verifier import (
    EntityVerification,
    ExpectedTransition,
    StateVerifier,
    VerificationResult,
    Watch,
    expected_from_service_call,
)


class _FakeHAClient:
    """Minimal HAClient stand-in that lets tests push synthetic
    state_changed events at registered callbacks."""

    def __init__(self) -> None:
        self._cbs: list[Callable[[dict[str, Any]], None]] = []

    def on_state_changed(self, cb: Callable[[dict[str, Any]], None]) -> None:
        self._cbs.append(cb)

    def off_state_changed(self, cb: Callable[[dict[str, Any]], None]) -> None:
        try:
            self._cbs.remove(cb)
        except ValueError:
            pass

    # Test helper: fire a state_changed event synchronously.
    def emit(self, entity_id: str, state: str, attrs: dict | None = None) -> None:
        event = {
            "entity_id": entity_id,
            "new_state": {
                "entity_id": entity_id,
                "state": state,
                "attributes": attrs or {},
            },
        }
        for cb in list(self._cbs):
            cb(event)


class _StubCache:
    def get(self, entity_id: str):  # noqa: ARG002
        return None


# ───────────────────────────────────────────────────────────────
# Expected-transition inference
# ───────────────────────────────────────────────────────────────

class TestInferExpected:
    def test_turn_on_maps_to_state_on(self) -> None:
        out = expected_from_service_call(
            "light", "turn_on", ["light.x"],
        )
        assert out[0].expected_state == "on"
        assert not out[0].skip_verification

    def test_turn_off_maps_to_state_off(self) -> None:
        out = expected_from_service_call(
            "light", "turn_off", ["light.x"],
        )
        assert out[0].expected_state == "off"

    def test_toggle_accepts_any_state(self) -> None:
        out = expected_from_service_call(
            "light", "toggle", ["light.x"],
        )
        assert out[0].expected_state is None
        assert not out[0].skip_verification

    def test_scene_turn_on_is_skipped(self) -> None:
        """Scenes don't produce observable state on themselves —
        verification must mark them as skipped."""
        out = expected_from_service_call(
            "scene", "turn_on", ["scene.evening"],
        )
        assert out[0].skip_verification

    def test_script_turn_on_is_skipped(self) -> None:
        out = expected_from_service_call(
            "script", "turn_on", ["script.bedtime"],
        )
        assert out[0].skip_verification

    def test_brightness_pct_translated_to_brightness_0_255(self) -> None:
        """HA state reports the light's `brightness` attribute on the
        0-255 scale — the `brightness_pct` name only exists in service
        calls. The verifier must translate or every % command will
        spuriously fail."""
        out = expected_from_service_call(
            "light", "turn_on", ["light.x"],
            service_data={"brightness_pct": 50},
        )
        # 50% ≈ 128 on the 0-255 scale.
        assert out[0].expected_attrs.get("brightness") == 128
        assert "brightness_pct" not in out[0].expected_attrs
        assert out[0].attr_tolerance.get("brightness", 0) > 0

    def test_brightness_pct_10_translates_to_brightness_26(self) -> None:
        # Regression for the live incident where "Set the desk lamp to
        # 10%" produced mismatch_reason="brightness_pct=None (want 10)".
        out = expected_from_service_call(
            "light", "turn_on", ["light.x"],
            service_data={"brightness_pct": 10},
        )
        assert out[0].expected_attrs.get("brightness") == 26  # round(10*255/100)

    def test_color_temp_tolerance_is_generous(self) -> None:
        out = expected_from_service_call(
            "light", "turn_on", ["light.x"],
            service_data={"color_temp_kelvin": 2700},
        )
        tol = out[0].attr_tolerance.get("color_temp_kelvin", 0)
        # Bulbs bucket to nearest supported kelvin; 200K tolerance
        # catches that without letting a wildly wrong value pass.
        assert 50 <= tol <= 500

    def test_color_name_compared_exactly(self) -> None:
        out = expected_from_service_call(
            "light", "turn_on", ["light.x"],
            service_data={"color_name": "blue"},
        )
        assert out[0].expected_attrs["color_name"] == "blue"
        assert "color_name" not in out[0].attr_tolerance

    def test_unknown_service_skips_verification(self) -> None:
        """Unknown services get skipped rather than failing the
        whole verification chain."""
        out = expected_from_service_call(
            "input_text", "set_value", ["input_text.x"],
            service_data={"value": "hello"},
        )
        assert out[0].skip_verification

    def test_multiple_entity_ids_produce_list(self) -> None:
        out = expected_from_service_call(
            "light", "turn_off", ["light.a", "light.b", "light.c"],
        )
        assert len(out) == 3
        assert all(t.expected_state == "off" for t in out)


# ───────────────────────────────────────────────────────────────
# Watch + StateVerifier
# ───────────────────────────────────────────────────────────────

class TestWatchHappyPath:
    def test_state_change_satisfies_single_entity(self) -> None:
        ha = _FakeHAClient()
        verifier = StateVerifier(ha, _StubCache())
        watch = verifier.begin_watch(
            [ExpectedTransition(entity_id="light.x", expected_state="on")],
            timeout_s=2.0,
        )
        # Simulate HA firing the transition we expected.
        ha.emit("light.x", "on")
        result = watch.wait()
        assert result.verified
        assert not result.timed_out
        assert result.per_entity["light.x"].verified

    def test_all_entities_must_verify(self) -> None:
        ha = _FakeHAClient()
        verifier = StateVerifier(ha, _StubCache())
        watch = verifier.begin_watch(
            [
                ExpectedTransition(entity_id="light.a", expected_state="on"),
                ExpectedTransition(entity_id="light.b", expected_state="on"),
            ],
            timeout_s=0.5,
        )
        # Only one entity moves. The other never does.
        ha.emit("light.a", "on")
        result = watch.wait()
        assert not result.verified
        assert result.timed_out
        assert result.per_entity["light.a"].verified
        assert not result.per_entity["light.b"].verified
        assert "light.b" in result.failed_entity_ids

    def test_attribute_tolerance_accepts_near_match(self) -> None:
        ha = _FakeHAClient()
        verifier = StateVerifier(ha, _StubCache())
        watch = verifier.begin_watch(
            [ExpectedTransition(
                entity_id="light.x",
                expected_state="on",
                expected_attrs={"brightness_pct": 50},
                attr_tolerance={"brightness_pct": 5.0},
            )],
            timeout_s=0.5,
        )
        # HA reports 48 instead of 50 — within tolerance.
        ha.emit("light.x", "on", {"brightness_pct": 48})
        result = watch.wait()
        assert result.verified

    def test_attribute_out_of_tolerance_fails(self) -> None:
        ha = _FakeHAClient()
        verifier = StateVerifier(ha, _StubCache())
        watch = verifier.begin_watch(
            [ExpectedTransition(
                entity_id="light.x",
                expected_state="on",
                expected_attrs={"brightness_pct": 50},
                attr_tolerance={"brightness_pct": 5.0},
            )],
            timeout_s=0.3,
        )
        # HA reports 75 — way outside tolerance.
        ha.emit("light.x", "on", {"brightness_pct": 75})
        result = watch.wait()
        assert not result.verified
        assert result.per_entity["light.x"].mismatch_reason
        assert "brightness_pct" in result.per_entity["light.x"].mismatch_reason

    def test_toggle_accepts_any_state_change(self) -> None:
        ha = _FakeHAClient()
        verifier = StateVerifier(ha, _StubCache())
        watch = verifier.begin_watch(
            [ExpectedTransition(entity_id="light.x")],  # any state ok
            timeout_s=0.5,
        )
        ha.emit("light.x", "off")  # any transition counts
        result = watch.wait()
        assert result.verified


class TestWatchTimeoutAndEdgeCases:
    def test_no_event_times_out(self) -> None:
        ha = _FakeHAClient()
        verifier = StateVerifier(ha, _StubCache())
        watch = verifier.begin_watch(
            [ExpectedTransition(entity_id="light.x", expected_state="on")],
            timeout_s=0.2,  # tight
        )
        # Nothing emitted.
        t0 = time.monotonic()
        result = watch.wait()
        elapsed = time.monotonic() - t0
        assert not result.verified
        assert result.timed_out
        assert 0.1 <= elapsed < 1.0
        assert result.per_entity["light.x"].mismatch_reason == "no state_changed observed"

    def test_all_skipped_returns_immediately_verified_not_required(self) -> None:
        """When every expected transition has skip_verification=True
        (e.g. scene.turn_on), wait() returns right away with
        verified=True (nothing to fail)."""
        ha = _FakeHAClient()
        verifier = StateVerifier(ha, _StubCache())
        watch = verifier.begin_watch(
            [ExpectedTransition(entity_id="scene.x", skip_verification=True)],
            timeout_s=5.0,  # large timeout to prove we don't wait
        )
        t0 = time.monotonic()
        result = watch.wait()
        elapsed = time.monotonic() - t0
        assert result.verified
        assert not result.timed_out
        assert elapsed < 0.1
        assert result.per_entity["scene.x"].skipped
        assert result.any_skipped

    def test_unrelated_state_changes_ignored(self) -> None:
        ha = _FakeHAClient()
        verifier = StateVerifier(ha, _StubCache())
        watch = verifier.begin_watch(
            [ExpectedTransition(entity_id="light.x", expected_state="on")],
            timeout_s=0.3,
        )
        # A flood of other-entity events must not satisfy.
        for _ in range(20):
            ha.emit("light.other", "on")
        result = watch.wait()
        assert not result.verified
        assert result.timed_out

    def test_callback_is_unregistered_after_wait(self) -> None:
        ha = _FakeHAClient()
        verifier = StateVerifier(ha, _StubCache())
        watch = verifier.begin_watch(
            [ExpectedTransition(entity_id="light.x", expected_state="on")],
            timeout_s=0.2,
        )
        assert len(ha._cbs) == 1
        watch.wait()
        assert len(ha._cbs) == 0, "callback leaked after wait()"

    def test_late_event_after_wait_does_not_crash(self) -> None:
        ha = _FakeHAClient()
        verifier = StateVerifier(ha, _StubCache())
        watch = verifier.begin_watch(
            [ExpectedTransition(entity_id="light.x", expected_state="on")],
            timeout_s=0.2,
        )
        result = watch.wait()
        assert not result.verified
        # Emit after wait() returned — callback was unregistered,
        # should be a no-op.
        ha.emit("light.x", "on")  # must not raise


class TestVerificationResultAPI:
    def test_failed_entity_ids_lists_only_unverified(self) -> None:
        result = VerificationResult(
            verified=False,
            timed_out=True,
            elapsed_s=3.0,
            per_entity={
                "light.a": EntityVerification("light.a", verified=True),
                "light.b": EntityVerification("light.b", verified=False),
                "scene.c": EntityVerification("scene.c", verified=False, skipped=True),
            },
        )
        assert result.failed_entity_ids == ["light.b"]
        assert result.any_skipped

    def test_verified_true_when_all_pass_or_skipped(self) -> None:
        result = VerificationResult(
            verified=True,
            timed_out=False,
            elapsed_s=0.1,
            per_entity={
                "light.a": EntityVerification("light.a", verified=True),
                "scene.b": EntityVerification("scene.b", verified=False, skipped=True),
            },
        )
        assert result.verified
        assert result.failed_entity_ids == []


class TestThreadedCallback:
    """Verify the watch correctly handles state_changed events fired
    from a different thread than wait() is called on — which is the
    real production scenario (ws_client fires on the asyncio thread,
    wait() blocks on the caller's thread)."""

    def test_event_from_other_thread_wakes_waiter(self) -> None:
        ha = _FakeHAClient()
        verifier = StateVerifier(ha, _StubCache())
        watch = verifier.begin_watch(
            [ExpectedTransition(entity_id="light.x", expected_state="on")],
            timeout_s=2.0,
        )

        def _emit_after_delay() -> None:
            time.sleep(0.05)
            ha.emit("light.x", "on")

        threading.Thread(target=_emit_after_delay, daemon=True).start()
        t0 = time.monotonic()
        result = watch.wait()
        elapsed = time.monotonic() - t0
        assert result.verified
        assert elapsed < 1.5  # didn't wait out the full timeout
        assert elapsed >= 0.04
