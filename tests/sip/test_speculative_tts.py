"""Tests for glados.sip.speculative_tts."""
from __future__ import annotations

import asyncio
import time

import pytest

from glados.sip.speculative_tts import SpeculativeTtsCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_tts(delay_s: float = 0.0):
    """Build a mock TTS callable that returns the text bytes after ``delay_s``."""

    async def synth(text: str) -> bytes:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        return f"audio[{text}]".encode()

    return synth


# ---------------------------------------------------------------------------
# consume — happy paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consume_returns_cached_audio_after_ready() -> None:
    """If the speculative task finishes before consume, we get cached bytes."""
    cache = SpeculativeTtsCache(_mock_tts(delay_s=0.05))
    cache.register_branch("pin_entry", {
        "pin_success": "Acknowledged",
        "pin_fail_1": "Wrong, two left",
    })
    # Wait long enough for both renders to finish
    await asyncio.sleep(0.15)
    out = await cache.consume("pin_entry", "pin_success")
    assert out == b"audio[Acknowledged]"


@pytest.mark.asyncio
async def test_consume_awaits_in_flight_task() -> None:
    """If consume is called before the task finishes, it awaits."""
    cache = SpeculativeTtsCache(_mock_tts(delay_s=0.2))
    cache.register_branch("pin_entry", {"pin_success": "Acknowledged"})
    # Consume immediately — task is still running
    t0 = time.monotonic()
    out = await cache.consume("pin_entry", "pin_success")
    elapsed = time.monotonic() - t0
    assert out == b"audio[Acknowledged]"
    # Should have waited for the in-flight task (~0.2s), not started fresh
    assert elapsed < 0.5  # allow generous slack


@pytest.mark.asyncio
async def test_consume_cache_hit_fast() -> None:
    """Cache hit should be near-instant (the demo from the spec)."""
    cache = SpeculativeTtsCache(_mock_tts(delay_s=2.0))  # slow TTS
    cache.register_branch("menu_idle", {"menu_item_1": "House status"})
    await asyncio.sleep(2.1)  # let it complete
    t0 = time.monotonic()
    out = await cache.consume("menu_idle", "menu_item_1")
    elapsed = time.monotonic() - t0
    assert out == b"audio[House status]"
    # Should be near-instant — the TTS call is already done
    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_consume_unknown_branch_falls_back_to_sync() -> None:
    """If the branch isn't registered, fall back to fallback_text."""
    cache = SpeculativeTtsCache(_mock_tts(delay_s=0.0))
    out = await cache.consume("nonexistent", "pin_success", fallback_text="Hello")
    assert out == b"audio[Hello]"


@pytest.mark.asyncio
async def test_consume_unknown_label_falls_back() -> None:
    cache = SpeculativeTtsCache(_mock_tts(delay_s=0.0))
    cache.register_branch("pin_entry", {"pin_success": "Yes"})
    out = await cache.consume("pin_entry", "different_label", fallback_text="Hello")
    assert out == b"audio[Hello]"


@pytest.mark.asyncio
async def test_consume_no_fallback_raises_keyerror() -> None:
    cache = SpeculativeTtsCache(_mock_tts(delay_s=0.0))
    with pytest.raises(KeyError):
        await cache.consume("nonexistent", "label")


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_other_kills_siblings_keeps_match() -> None:
    cache = SpeculativeTtsCache(_mock_tts(delay_s=0.5))
    cache.register_branch("pin_entry", {
        "pin_success": "yes",
        "pin_fail_1": "no",
        "pin_fail_2": "no2",
    })
    cache.cancel_other("pin_entry", "pin_success")
    await asyncio.sleep(0.05)  # give cancellation time to propagate
    stats = cache.stats()
    # Survivors map only contains pin_success
    assert "pin_entry" in stats
    assert "pin_success" in stats["pin_entry"]
    assert "pin_fail_1" not in stats["pin_entry"]
    assert "pin_fail_2" not in stats["pin_entry"]


@pytest.mark.asyncio
async def test_cancel_other_unknown_branch_is_noop() -> None:
    cache = SpeculativeTtsCache(_mock_tts(delay_s=0.0))
    cache.cancel_other("nonexistent", "label")  # should not raise


@pytest.mark.asyncio
async def test_cancel_branch_clears_all() -> None:
    cache = SpeculativeTtsCache(_mock_tts(delay_s=0.5))
    cache.register_branch("pin_entry", {"a": "1", "b": "2", "c": "3"})
    cache.cancel_branch("pin_entry")
    await asyncio.sleep(0.05)
    assert cache.stats() == {}


@pytest.mark.asyncio
async def test_cancel_all_clears_every_branch() -> None:
    cache = SpeculativeTtsCache(_mock_tts(delay_s=0.5))
    cache.register_branch("pin_entry", {"a": "1"})
    cache.register_branch("menu_idle", {"x": "100"})
    cache.cancel_all()
    await asyncio.sleep(0.05)
    assert cache.stats() == {}


@pytest.mark.asyncio
async def test_consume_after_cancel_falls_back() -> None:
    """Consuming a label that was just cancelled should fall back to sync TTS."""
    cache = SpeculativeTtsCache(_mock_tts(delay_s=0.5))
    cache.register_branch("pin_entry", {"pin_success": "yes"})
    cache.cancel_branch("pin_entry")
    await asyncio.sleep(0.05)
    out = await cache.consume("pin_entry", "pin_success", fallback_text="fallback")
    # Branch was cancelled → falls back to sync render of fallback_text
    assert out == b"audio[fallback]"


# ---------------------------------------------------------------------------
# Concurrency cap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrency_cap_serializes_extra_jobs() -> None:
    """With max_concurrent=2 and 5 jobs, only 2 run at a time."""
    in_flight = 0
    max_seen = 0
    lock = asyncio.Lock()

    async def tracking_tts(text: str) -> bytes:
        nonlocal in_flight, max_seen
        async with lock:
            in_flight += 1
            max_seen = max(max_seen, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return f"audio[{text}]".encode()

    cache = SpeculativeTtsCache(tracking_tts, max_concurrent=2)
    cache.register_branch("burst", {f"job_{i}": str(i) for i in range(5)})
    await asyncio.sleep(0.5)  # let all complete
    assert max_seen == 2  # never exceeded the cap


@pytest.mark.asyncio
async def test_register_branch_replaces_existing_branch() -> None:
    cache = SpeculativeTtsCache(_mock_tts(delay_s=0.5))
    cache.register_branch("pin_entry", {"old_a": "1", "old_b": "2"})
    cache.register_branch("pin_entry", {"new_x": "100"})
    await asyncio.sleep(0.05)
    stats = cache.stats()
    assert "old_a" not in stats.get("pin_entry", {})
    assert "old_b" not in stats.get("pin_entry", {})
    assert "new_x" in stats["pin_entry"]


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_render_falls_back_on_consume() -> None:
    """If the speculative render raises, consume falls back to fallback_text."""

    async def failing_tts(text: str) -> bytes:
        if text == "boom":
            raise RuntimeError("intentional TTS failure")
        return f"audio[{text}]".encode()

    cache = SpeculativeTtsCache(failing_tts)
    cache.register_branch("pin_entry", {"pin_success": "boom"})
    await asyncio.sleep(0.05)
    # The cached task failed; consume should fall back
    out = await cache.consume("pin_entry", "pin_success", fallback_text="ok")
    assert out == b"audio[ok]"
