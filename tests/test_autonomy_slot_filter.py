"""Tests for the autonomy slot-filter gates that keep routine ticks
small.

Two helpers in `glados.core.llm_processor`:

1. ``_should_render_slot(slot)`` — whether the slot's line should
   appear in the autonomy tick prompt's ``Tasks:`` summary. Passive
   monitoring noise (compaction at 1700/6000, camera watcher in error,
   subagent idle/done) is filtered out unless the slot has an
   importance set ≥ 0.4.

2. ``_autonomy_has_actionable_slot(slot_store)`` — whether at least
   one autonomy slot is actionable (importance ≥ 0.6, the threshold
   the autonomy system prompt calls out). Used by the tool-filter
   gate to force-strip the MCP catalog when no slot has actually
   flagged something to act on.

Both gates exist to defeat the failure mode where slot names
themselves trip the chat-shape filter (e.g. a persistent "Camera
Watcher" slot puts 'camera' in every tick, keeping the full ~95-tool
catalog forever).
"""

from __future__ import annotations

import time

from glados.autonomy.slots import TaskSlot, TaskSlotStore
from glados.core.llm_processor import (
    _autonomy_has_actionable_slot,
    _should_render_slot,
)


def _slot(
    title: str,
    status: str,
    *,
    importance: float | None = None,
    summary: str = "",
) -> TaskSlot:
    return TaskSlot(
        slot_id=title.lower().replace(" ", "_"),
        title=title,
        status=status,
        summary=summary,
        updated_at=time.time(),
        importance=importance,
    )


# ── _should_render_slot — passive states are filtered ─────────────────


def test_compaction_monitoring_filtered():
    s = _slot("Message Compaction", "monitoring", summary="Context at 1700 tokens")
    assert _should_render_slot(s) is False


def test_camera_watcher_error_filtered():
    s = _slot("Camera Watcher", "error", summary="vision service unreachable")
    assert _should_render_slot(s) is False


def test_subagent_idle_filtered():
    s = _slot("Memory Classifier", "idle")
    assert _should_render_slot(s) is False


def test_subagent_done_filtered():
    s = _slot("Camera Watcher", "done", summary="No new camera events")
    assert _should_render_slot(s) is False


def test_subagent_running_filtered():
    s = _slot("Hacker News", "running")
    assert _should_render_slot(s) is False


# ── _should_render_slot — actionable / non-passive states are kept ────


def test_alert_status_kept():
    s = _slot("Camera Watcher", "alert", summary="Person detected at front_door")
    assert _should_render_slot(s) is True


def test_active_emotional_state_kept():
    s = _slot("Emotional State", "active", summary="Contemptuous Calm (0.50)")
    assert _should_render_slot(s) is True


def test_high_importance_overrides_passive_status():
    """Even if a subagent emits a routine status string, if it sets
    importance >= 0.4 the slot is kept — the importance is the
    operator-meaningful actionability signal."""
    s = _slot("Doorbell", "monitoring", importance=0.9)
    assert _should_render_slot(s) is True


def test_low_importance_doesnt_save_passive_status():
    s = _slot("Camera Watcher", "monitoring", importance=0.2)
    assert _should_render_slot(s) is False


# ── _autonomy_has_actionable_slot — severity gate ─────────────────────


def test_actionable_when_slot_importance_at_threshold():
    store = TaskSlotStore()
    store.update_slot(
        slot_id="doorbell", title="Doorbell", status="alert",
        summary="ringing", importance=0.6,
    )
    assert _autonomy_has_actionable_slot(store) is True


def test_actionable_when_slot_importance_above_threshold():
    store = TaskSlotStore()
    store.update_slot(
        slot_id="cam", title="Camera Watcher", status="alert",
        summary="Person at front_door", importance=0.9,
    )
    assert _autonomy_has_actionable_slot(store) is True


def test_not_actionable_when_only_routine_slots():
    """The kitchen-routine baseline: persistent monitoring slots,
    none flagged. Tool catalog should drop."""
    store = TaskSlotStore()
    store.update_slot(
        slot_id="compact", title="Message Compaction", status="monitoring",
        summary="Context at 1700 tokens",
    )
    store.update_slot(
        slot_id="cam", title="Camera Watcher", status="error",
        summary="vision service unreachable",
    )
    store.update_slot(
        slot_id="emot", title="Emotional State", status="active",
        summary="Contemptuous Calm",
    )
    assert _autonomy_has_actionable_slot(store) is False


def test_not_actionable_when_below_threshold():
    """Routine-importance subagent updates aren't strong enough."""
    store = TaskSlotStore()
    store.update_slot(
        slot_id="cam", title="Camera Watcher", status="done",
        summary="No new camera events", importance=0.2,
    )
    assert _autonomy_has_actionable_slot(store) is False


def test_not_actionable_when_no_store():
    assert _autonomy_has_actionable_slot(None) is False


def test_not_actionable_when_empty_store():
    assert _autonomy_has_actionable_slot(TaskSlotStore()) is False
