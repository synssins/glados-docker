"""Call session — state-machine glue for one inbound SIP call.

Owns the lifecycle of a single call from INVITE arrival through BYE,
coordinating every other ``glados.sip`` module:

- ``ctrl_client`` — gives us call_started / dtmf / call_ended events
- ``audio_bridge`` — pumps PCM to STT, plays TTS bytes back
- ``pin_gate`` — gates entry on a 4-digit PIN
- ``ivr`` — DTMF menu post-PIN
- ``speculative_tts`` — pre-render the next likely audio
- ``recording`` — captures audio + transcript + metadata
- ``persona`` — system-prompt fragment + canned screening responses

State machine:

::

    IDLE ─INVITE──▶ RINGING ─answer──▶ ESTABLISHED
                                            │
                                            ▼
                                       GREETING (TTS)
                                            │
                                            ▼
                                       PIN_ENTRY (STT + DTMF parallel)
                                  ┌─────────┴─────────┐
                                  │                   │
                            valid PIN              3 failures
                                  │                   │
                                  ▼                   ▼
                            ivr_menu.enabled?    REJECT (TTS) → BYE
                           ┌─────┴─────┐
                           ▼           ▼
                         MENU      CONVERSATION
                           │           │
                       drop key        │
                       (0)             │
                           ▼           │
                      CONVERSATION ────┘
                                       │
                                       ▼ caller BYE
                                    cleanup (save recording, prune FIFO)

NOTE: transport between baresip and audio_bridge is abstract here —
the bridge takes asyncio.StreamReader/Writer pair. See Task 5's
spec rev for why (baresip's stock aufile is WAV-only). The actual
transport implementation (custom baresip C module exposing PCM over
a Unix domain socket OR Python RTP with baresip handling only SIP
signaling) is the one remaining production decision before Slice 1
can ship live.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from loguru import logger

from glados.sip.audio_bridge import AudioBridge
from glados.sip.config import SipConfig
from glados.sip.ivr import IvrController, IvrExit
from glados.sip.persona import (
    CANNED_TEXT,
    bake_canned_responses,
    get_canned_text,
)
from glados.sip.pin_gate import GateResult, PinGate
from glados.sip.recording import CallRecording
from glados.sip.speculative_tts import SpeculativeTtsCache


class CallState(str, Enum):
    """Top-level state machine states. Tracked for logging + audit."""
    IDLE = "idle"
    RINGING = "ringing"
    ESTABLISHED = "established"
    GREETING = "greeting"
    PIN_ENTRY = "pin_entry"
    MENU = "menu"
    CONVERSATION = "conversation"
    REJECTED = "rejected"
    BYE = "bye"


@dataclass
class CallSessionDeps:
    """Injected dependencies — kept in one struct for legibility.

    Production wiring is in ``glados.sip`` package init / engine
    bootstrap; tests inject mocks for each.
    """
    cfg: SipConfig
    audio_bridge: AudioBridge
    speculative_cache: SpeculativeTtsCache
    recording: CallRecording

    # TTS callable — synthesise text → audio bytes (8 kHz int16 PCM)
    tts: Callable[[str], Awaitable[bytes]]

    # Engine for free-form CONVERSATION turns. process(text) → response text.
    engine_process: Callable[[str], Awaitable[str]]

    # Hangup hook — when state machine wants the call to end, this asks
    # ctrl_client to send BYE on the live SIP dialog.
    hangup: Callable[[], Awaitable[None]]


class CallSession:
    """One active inbound call.

    Use:
        sess = CallSession(deps)
        await sess.run()
    """

    def __init__(self, deps: CallSessionDeps) -> None:
        self._deps = deps
        self._cfg = deps.cfg
        self.state = CallState.IDLE

        # Caller-side input streams. PIN gate consumes both; ivr and
        # conversation consume DTMF alone.
        self._dtmf_queue: asyncio.Queue[str] = asyncio.Queue()
        self._stt_queue: asyncio.Queue[str] = asyncio.Queue()

        # Set when a BYE is received from the remote side.
        self._bye_event = asyncio.Event()

        # PIN gate state — built fresh on entry to PIN_ENTRY
        self._gate: PinGate | None = None

    # ------------------------------------------------------------------
    # Public event injection (called by ctrl_client subscriptions)
    # ------------------------------------------------------------------

    def feed_dtmf(self, digit: str) -> None:
        """Push a DTMF digit into the call. Called by ctrl_client."""
        self._dtmf_queue.put_nowait(digit)

    def feed_stt(self, transcript: str) -> None:
        """Push an STT transcript chunk into the call."""
        if transcript:
            self._stt_queue.put_nowait(transcript)

    def signal_bye(self) -> None:
        """Mark the call as ended by the remote side. Called by ctrl_client."""
        self._bye_event.set()

    # ------------------------------------------------------------------
    # Top-level run
    # ------------------------------------------------------------------

    async def run(self) -> CallState:
        """Drive the state machine until terminal. Returns final state."""
        log = logger.bind(group="sip")
        try:
            self.state = CallState.RINGING
            self._deps.recording.update_metadata(state_at_pickup="ringing")
            await self._transition_to(CallState.ESTABLISHED)

            # Speculative pre-render of PIN-entry branch
            self._register_pin_entry_speculative()

            await self._do_greeting()

            gate_result = await self._do_pin_entry()
            if gate_result is GateResult.FAIL:
                self.state = CallState.REJECTED
                self._deps.recording.update_metadata(pin_outcome="rejected")
                await self._play_canned("pin_fail_final")
                await self._end_call()
                return self.state
            self._deps.recording.update_metadata(pin_outcome="accepted")
            await self._play_canned("pin_success")

            # Decide MENU vs straight to CONVERSATION
            if self._cfg.inbound.ivr_menu.enabled:
                ivr_exit = await self._do_menu()
                if ivr_exit is IvrExit.HANGUP:
                    await self._play_canned("menu_no_input_hangup")
                    await self._end_call()
                    return self.state
                # FREEFORM — drop into conversation
                await self._play_canned("drop_to_freeform")

            await self._do_conversation()
            await self._end_call()
            return self.state

        except asyncio.CancelledError:
            log.info("call_session: cancelled")
            await self._end_call()
            raise
        except Exception as e:
            log.exception(f"call_session: unhandled error: {e}")
            await self._end_call()
            return self.state

    # ------------------------------------------------------------------
    # State transitions — internal
    # ------------------------------------------------------------------

    async def _transition_to(self, new_state: CallState) -> None:
        prev = self.state
        self.state = new_state
        logger.bind(group="sip").debug(f"call_session: {prev} → {new_state}")

    async def _do_greeting(self) -> None:
        await self._transition_to(CallState.GREETING)
        text = get_canned_text("greeting")
        self._deps.recording.append_transcript("GLaDOS", text)
        await self._play_canned("greeting")

    async def _do_pin_entry(self) -> GateResult:
        await self._transition_to(CallState.PIN_ENTRY)
        self._gate = PinGate(
            expected_pin=self._cfg.inbound.pin,
            max_failures=self._cfg.inbound.pin_failures_max,
        )
        # Drain DTMF + STT queues concurrently until the gate resolves
        async def consume_dtmf() -> None:
            while not self._gate.resolved and not self._bye_event.is_set():
                digit = await self._dtmf_queue.get()
                self._handle_gate_step(self._gate.feed_dtmf(digit))

        async def consume_stt() -> None:
            while not self._gate.resolved and not self._bye_event.is_set():
                transcript = await self._stt_queue.get()
                self._deps.recording.append_transcript("Caller", transcript)
                self._handle_gate_step(self._gate.feed_stt(transcript))

        dtmf_task = asyncio.create_task(consume_dtmf())
        stt_task = asyncio.create_task(consume_stt())

        try:
            # Wait until either gate resolves or BYE arrives
            while not self._gate.resolved and not self._bye_event.is_set():
                await asyncio.sleep(0.05)
        finally:
            dtmf_task.cancel()
            stt_task.cancel()
            async with _suppress():
                await dtmf_task
            async with _suppress():
                await stt_task

        self._deps.recording.update_metadata(pin_attempts=self._gate.failures + (1 if self._gate.resolved else 0))
        if self._bye_event.is_set():
            return GateResult.FAIL  # caller hung up mid-PIN
        if self._gate is None:
            return GateResult.FAIL
        return GateResult.PASS if self._gate.failures < self._cfg.inbound.pin_failures_max and self._gate.resolved else GateResult.FAIL

    def _handle_gate_step(self, result: GateResult) -> None:
        """React to each pin_gate step result (other than terminal)."""
        if result is GateResult.INVALID:
            assert self._gate is not None
            attempts_left = self._gate.attempts_remaining
            # Play the appropriate fail variant via speculative cache
            label = (
                "pin_fail_1" if attempts_left == 2
                else "pin_fail_2" if attempts_left == 1
                else "pin_fail_final"
            )
            asyncio.create_task(self._play_canned(label))

    async def _do_menu(self) -> IvrExit:
        await self._transition_to(CallState.MENU)
        ivr = IvrController(
            items=self._cfg.inbound.ivr_menu.items,
            drop_dtmf=self._cfg.inbound.ivr_menu.drop_to_freeform_dtmf,
        )

        async def play_menu_prompt() -> None:
            # The menu prompt itself can be pre-rendered as a canned
            # response. Falls back to synth on miss.
            audio = await self._deps.speculative_cache.consume(
                "menu_prompt", "default",
                fallback_text=self._build_menu_prompt_text(),
            )
            await self._deps.audio_bridge.write_outbound(audio)

        async def play_handler_response(handler_name: str) -> None:
            # Speculative cache stores handler responses under a per-handler
            # label. If not pre-rendered, fall back to "Working on it" + sync.
            audio = await self._deps.speculative_cache.consume(
                "menu_idle", handler_name,
                fallback_text=f"Looking up {handler_name.replace('_', ' ')}...",
            )
            await self._deps.audio_bridge.write_outbound(audio)

        async def get_dtmf(timeout_s: float) -> Optional[str]:
            try:
                return await asyncio.wait_for(self._dtmf_queue.get(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return None

        return await ivr.run(
            play_prompt=play_menu_prompt,
            play_handler_response=play_handler_response,
            get_dtmf=get_dtmf,
        )

    async def _do_conversation(self) -> None:
        await self._transition_to(CallState.CONVERSATION)
        log = logger.bind(group="sip")
        # Drain STT until BYE; route each utterance through the engine
        while not self._bye_event.is_set():
            try:
                utterance = await asyncio.wait_for(self._stt_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not utterance:
                continue
            self._deps.recording.append_transcript("Caller", utterance)
            try:
                response = await self._deps.engine_process(utterance)
            except Exception as e:
                log.exception(f"engine_process failed: {e}")
                response = "Something is broken on my end. Goodbye."
                self._deps.recording.append_transcript("GLaDOS", response)
                await self._tts_and_play(response)
                break
            self._deps.recording.append_transcript("GLaDOS", response)
            await self._tts_and_play(response)

    async def _end_call(self) -> None:
        await self._transition_to(CallState.BYE)
        async with _suppress():
            await self._deps.hangup()
        async with _suppress():
            await self._deps.audio_bridge.stop()
        async with _suppress():
            await self._deps.recording.close()

    # ------------------------------------------------------------------
    # Helpers — audio playback + TTS
    # ------------------------------------------------------------------

    async def _play_canned(self, label: str) -> None:
        """Play a canned response; mute STT during playback."""
        audio = await self._deps.speculative_cache.consume(
            "pin_entry", label, fallback_text=get_canned_text(label),
        )
        await self._play_audio(audio)

    async def _tts_and_play(self, text: str) -> None:
        audio = await self._deps.tts(text)
        await self._play_audio(audio)

    async def _play_audio(self, audio: bytes) -> None:
        self._deps.audio_bridge.set_tts_active(True)
        try:
            await self._deps.audio_bridge.write_outbound(audio)
            # Estimate playback duration so we know when to clear self-listen
            # mute. 8 kHz int16 = 16000 bytes/second. Add 100 ms safety.
            seconds = len(audio) / 16000 + 0.1
            await asyncio.sleep(seconds)
        finally:
            self._deps.audio_bridge.set_tts_active(False)

    def _register_pin_entry_speculative(self) -> None:
        """Pre-render the four likely PIN responses while greeting plays."""
        self._deps.speculative_cache.register_branch(
            "pin_entry",
            {label: get_canned_text(label) for label in (
                "pin_success", "pin_fail_1", "pin_fail_2", "pin_fail_final",
            )},
        )

    def _build_menu_prompt_text(self) -> str:
        """Build the IVR menu prompt from the configured items."""
        items = self._cfg.inbound.ivr_menu.items
        drop = self._cfg.inbound.ivr_menu.drop_to_freeform_dtmf
        if not items:
            return "No menu options. Press " + drop + " to talk to me directly."
        parts = [f"Press {_digit_word(item.key)} for {item.label.lower()}" for item in items]
        return ", ".join(parts) + f", or {_digit_word(drop)} to talk to me directly."


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------

_DIGIT_WORDS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
    "*": "star", "#": "pound",
}


def _digit_word(digit: str) -> str:
    return _DIGIT_WORDS.get(digit, digit)


class _suppress:
    """Async context manager that swallows exceptions on cleanup paths.

    We use this around the multi-step end_call path so a failure in one
    teardown step doesn't strand the others.
    """
    async def __aenter__(self) -> "_suppress":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return True
