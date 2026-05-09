"""IVR (Interactive Voice Response) controller for SIP inbound calls.

After the PIN gate succeeds, ``call_session`` hands control to the
IVR. The IVR loops:

    play menu prompt → wait for DTMF → dispatch handler → play
    response → loop

A configurable ``drop_dtmf`` digit (default ``0``) exits the IVR and
signals call_session to enter free-form CONVERSATION mode. After
``max_reprompts`` (default 3) consecutive timeout cycles with no
input, the IVR signals HANGUP.

The controller is pure state-machine logic — it doesn't know about
audio, RTP, or baresip. Caller injects three callables:

- ``play_prompt()`` — plays the pre-rendered menu prompt audio
- ``play_handler_response(handler_name)`` — runs the named handler
  + plays its TTS audio (call_session wires the handler-context +
  speculative-TTS cache here)
- ``get_dtmf(timeout_s)`` — awaits the next DTMF digit from
  ``ctrl_client``, returning ``None`` on timeout

That keeps the IVR testable with synthetic events, no audio
machinery required.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from loguru import logger

from glados.sip.config import SipIvrItem


class IvrExit(str, Enum):
    """Result code returned to call_session when the IVR loop exits."""
    FREEFORM = "freeform"   # caller pressed drop_dtmf — go to CONVERSATION
    HANGUP = "hangup"       # silence-timeout limit reached — send BYE


# Type aliases for the injected callables
PlayPrompt = Callable[[], Awaitable[None]]
PlayHandlerResponse = Callable[[str], Awaitable[None]]
GetDtmf = Callable[[float], Awaitable[Optional[str]]]


@dataclass
class IvrController:
    """IVR menu state machine.

    Construct with the menu config + caller-injected I/O callables;
    call ``run()``; await the ``IvrExit`` it returns.
    """

    items: list[SipIvrItem]
    drop_dtmf: str = "0"
    timeout_s: float = 10.0
    max_reprompts: int = 3

    async def run(
        self,
        *,
        play_prompt: PlayPrompt,
        play_handler_response: PlayHandlerResponse,
        get_dtmf: GetDtmf,
    ) -> IvrExit:
        """Run the menu loop until exit. Returns ``FREEFORM`` or ``HANGUP``."""
        log = logger.bind(group="sip")
        # Build a digit → item lookup once.
        item_by_key = {item.key: item for item in self.items}

        reprompts = 0
        while True:
            await play_prompt()

            try:
                digit = await get_dtmf(self.timeout_s)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception(f"ivr: get_dtmf raised: {e}")
                digit = None

            if digit is None:
                # Silence / timeout cycle
                reprompts += 1
                log.debug(f"ivr: silence reprompt {reprompts}/{self.max_reprompts}")
                if reprompts >= self.max_reprompts:
                    return IvrExit.HANGUP
                continue

            if digit == self.drop_dtmf:
                log.debug(f"ivr: drop key {digit!r} → FREEFORM")
                return IvrExit.FREEFORM

            item = item_by_key.get(digit)
            if item is None:
                # Unrecognised digit — counts as a reprompt cycle, but
                # don't penalise the operator for fat-fingering.
                log.debug(f"ivr: unknown digit {digit!r}; replaying prompt")
                continue

            # Valid menu hit — reset reprompt counter
            reprompts = 0
            log.debug(f"ivr: dispatch handler={item.handler!r} (key {digit!r}, label {item.label!r})")
            try:
                await play_handler_response(item.handler)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Handler failed — log and keep looping. The caller
                # may have already played an error message.
                log.exception(f"ivr: handler {item.handler!r} raised: {e}")
            # Loop back to play_prompt
