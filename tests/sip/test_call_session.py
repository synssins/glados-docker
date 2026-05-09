"""Tests for glados.sip.call_session.

We mock all injected dependencies (audio_bridge, speculative_cache,
recording, tts, engine, hangup) and feed scripted events. The state
machine's flow is what's under test, not the audio plumbing.

The deeper end-to-end exercise (real ctrl_client + real audio bridge
+ mock SIP server) is Task 13.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from glados.sip.call_session import CallSession, CallSessionDeps, CallState
from glados.sip.config import SipConfig, SipIvrItem


# ---------------------------------------------------------------------------
# Test helpers — build a CallSession with mock deps + canonical config
# ---------------------------------------------------------------------------

def _build_config(*, ivr_enabled: bool = True, items: list | None = None) -> SipConfig:
    return SipConfig(
        enabled=True,
        server={"host": "192.168.1.1", "username": "glados", "password": "x"},
        inbound={
            "pin": "8316",
            "pin_failures_max": 3,
            "ivr_menu": {
                "enabled": ivr_enabled,
                "drop_to_freeform_dtmf": "0",
                "items": items if items is not None else [
                    {"key": "1", "label": "House status", "handler": "house_status"},
                ],
            },
        },
    )


@dataclass
class _MockHarness:
    audio_writes: list = field(default_factory=list)
    transcript_lines: list[tuple[str, str]] = field(default_factory=list)
    metadata_updates: dict = field(default_factory=dict)
    recording_closed: bool = False
    hangup_called: bool = False
    bridge_stopped: bool = False
    tts_calls: list[str] = field(default_factory=list)
    engine_calls: list[str] = field(default_factory=list)


def _build_session(cfg: SipConfig | None = None,
                   tts_response: bytes = b"audio",
                   engine_response: str = "I see.") -> tuple[CallSession, _MockHarness]:
    cfg = cfg or _build_config()
    h = _MockHarness()

    audio_bridge = MagicMock()
    audio_bridge.write_outbound = AsyncMock(side_effect=lambda b: h.audio_writes.append(b))
    audio_bridge.set_tts_active = MagicMock()
    audio_bridge.stop = AsyncMock(side_effect=lambda: setattr(h, "bridge_stopped", True))

    spec_cache = MagicMock()

    async def consume(branch, label, *, fallback_text=None):
        return f"audio[{label}]".encode()

    spec_cache.consume = AsyncMock(side_effect=consume)
    spec_cache.register_branch = MagicMock()
    spec_cache.cancel_other = MagicMock()
    spec_cache.cancel_branch = MagicMock()
    spec_cache.cancel_all = MagicMock()

    recording = MagicMock()
    recording.append_audio = MagicMock()
    recording.append_transcript = MagicMock(
        side_effect=lambda speaker, text: h.transcript_lines.append((speaker, text)),
    )
    recording.update_metadata = MagicMock(
        side_effect=lambda **kw: h.metadata_updates.update(kw),
    )
    recording.close = AsyncMock(side_effect=lambda: setattr(h, "recording_closed", True))

    async def tts(text: str) -> bytes:
        h.tts_calls.append(text)
        return tts_response

    async def engine_process(text: str) -> str:
        h.engine_calls.append(text)
        return engine_response

    async def hangup() -> None:
        h.hangup_called = True

    deps = CallSessionDeps(
        cfg=cfg,
        audio_bridge=audio_bridge,
        speculative_cache=spec_cache,
        recording=recording,
        tts=tts,
        engine_process=engine_process,
        hangup=hangup,
    )
    sess = CallSession(deps)
    return sess, h


# ---------------------------------------------------------------------------
# PIN flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_pass_then_drop_to_conversation_then_bye() -> None:
    sess, h = _build_session()

    async def script() -> None:
        # Wait for PIN_ENTRY state
        while sess.state != CallState.PIN_ENTRY:
            await asyncio.sleep(0.01)
        # Send correct DTMF PIN
        for d in "8316":
            sess.feed_dtmf(d)
        # Wait for MENU
        while sess.state != CallState.MENU:
            await asyncio.sleep(0.01)
        # Press 0 to drop to free-form
        sess.feed_dtmf("0")
        # Wait for CONVERSATION
        while sess.state != CallState.CONVERSATION:
            await asyncio.sleep(0.01)
        # Caller speaks
        sess.feed_stt("What's the time?")
        await asyncio.sleep(0.1)
        # End the call
        sess.signal_bye()

    await asyncio.gather(sess.run(), script())

    assert sess.state == CallState.BYE
    assert h.metadata_updates.get("pin_outcome") == "accepted"
    # Transcript should include the greeting + caller utterance + GLaDOS reply
    speakers = [s for s, _ in h.transcript_lines]
    assert "GLaDOS" in speakers
    assert "Caller" in speakers
    assert "I see." in [t for _, t in h.transcript_lines]
    assert h.recording_closed
    assert h.hangup_called
    assert h.bridge_stopped


@pytest.mark.asyncio
async def test_pin_three_failures_rejects() -> None:
    sess, h = _build_session()

    async def script() -> None:
        while sess.state != CallState.PIN_ENTRY:
            await asyncio.sleep(0.01)
        # Three wrong attempts
        for _ in range(3):
            for d in "0000":
                sess.feed_dtmf(d)
            await asyncio.sleep(0.05)

    await asyncio.gather(sess.run(), script())

    assert sess.state == CallState.BYE
    assert h.metadata_updates.get("pin_outcome") == "rejected"
    assert h.recording_closed
    assert h.hangup_called


@pytest.mark.asyncio
async def test_bye_during_pin_entry_aborts() -> None:
    sess, h = _build_session()

    async def script() -> None:
        while sess.state != CallState.PIN_ENTRY:
            await asyncio.sleep(0.01)
        # Caller hangs up mid-PIN
        sess.signal_bye()

    await asyncio.gather(sess.run(), script())

    assert sess.state == CallState.BYE
    assert h.recording_closed


# ---------------------------------------------------------------------------
# Menu flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_menu_dispatch_then_drop() -> None:
    sess, h = _build_session()

    async def script() -> None:
        while sess.state != CallState.PIN_ENTRY:
            await asyncio.sleep(0.01)
        for d in "8316":
            sess.feed_dtmf(d)
        while sess.state != CallState.MENU:
            await asyncio.sleep(0.01)
        # Press 1 (handler dispatch), then 0 (drop)
        sess.feed_dtmf("1")
        await asyncio.sleep(0.05)  # let handler "play"
        sess.feed_dtmf("0")
        while sess.state != CallState.CONVERSATION:
            await asyncio.sleep(0.01)
        sess.signal_bye()

    await asyncio.gather(sess.run(), script())

    assert sess.state == CallState.BYE
    # Speculative cache was consumed at least for the menu_idle/handler path
    assert h.recording_closed


# ---------------------------------------------------------------------------
# IVR disabled — straight to conversation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ivr_disabled_pin_pass_goes_straight_to_conversation() -> None:
    cfg = _build_config(ivr_enabled=False)
    sess, h = _build_session(cfg=cfg)

    async def script() -> None:
        while sess.state != CallState.PIN_ENTRY:
            await asyncio.sleep(0.01)
        for d in "8316":
            sess.feed_dtmf(d)
        while sess.state != CallState.CONVERSATION:
            await asyncio.sleep(0.01)
        sess.signal_bye()

    await asyncio.gather(sess.run(), script())

    assert sess.state == CallState.BYE
    assert h.metadata_updates.get("pin_outcome") == "accepted"


# ---------------------------------------------------------------------------
# STT-driven PIN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stt_pin_works_alongside_dtmf() -> None:
    sess, h = _build_session()

    async def script() -> None:
        while sess.state != CallState.PIN_ENTRY:
            await asyncio.sleep(0.01)
        # Caller speaks the PIN instead of DTMFing
        sess.feed_stt("eight three one six")
        # Should advance through PIN to MENU
        while sess.state != CallState.MENU:
            await asyncio.sleep(0.01)
        sess.feed_dtmf("0")  # drop
        while sess.state != CallState.CONVERSATION:
            await asyncio.sleep(0.01)
        sess.signal_bye()

    await asyncio.gather(sess.run(), script())

    assert sess.state == CallState.BYE
    assert h.metadata_updates.get("pin_outcome") == "accepted"
    # The STT transcript was recorded
    assert any("eight three one six" in t for _, t in h.transcript_lines)
