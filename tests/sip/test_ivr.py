"""Tests for glados.sip.ivr — IVR menu state machine."""
from __future__ import annotations

import asyncio

import pytest

from glados.sip.config import SipIvrItem
from glados.sip.ivr import IvrController, IvrExit


# ---------------------------------------------------------------------------
# Helpers — synthetic event drivers
# ---------------------------------------------------------------------------

class _Driver:
    """Drives the IVR loop with scripted DTMF + records what was played."""

    def __init__(self, *, scripted_dtmf: list, scripted_timeouts: list[bool] = None):
        # scripted_dtmf and scripted_timeouts are zipped — each entry is one
        # 'wait for digit' cycle. If scripted_timeouts[i] is True, get_dtmf
        # returns None (silence). Otherwise scripted_dtmf[i] is the digit.
        self.scripted = scripted_dtmf
        self.timeouts = scripted_timeouts or [False] * len(scripted_dtmf)
        self.idx = 0
        self.prompts_played = 0
        self.handlers_played: list[str] = []

    async def play_prompt(self) -> None:
        self.prompts_played += 1

    async def play_handler_response(self, handler_name: str) -> None:
        self.handlers_played.append(handler_name)

    async def get_dtmf(self, _timeout_s: float):
        i = self.idx
        self.idx += 1
        if i >= len(self.scripted):
            return None  # exhausted script — treat as timeout
        if self.timeouts[i]:
            return None
        return self.scripted[i]


def _menu_items() -> list[SipIvrItem]:
    return [
        SipIvrItem(key="1", label="House status", handler="house_status"),
        SipIvrItem(key="2", label="Security state", handler="security_state"),
        SipIvrItem(key="3", label="Door locks", handler="door_locks"),
        SipIvrItem(key="4", label="Recent doorbell events", handler="doorbell_recent"),
    ]


# ---------------------------------------------------------------------------
# Drop key → FREEFORM
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drop_dtmf_returns_freeform() -> None:
    drv = _Driver(scripted_dtmf=["0"])
    ivr = IvrController(items=_menu_items(), drop_dtmf="0")
    result = await ivr.run(
        play_prompt=drv.play_prompt,
        play_handler_response=drv.play_handler_response,
        get_dtmf=drv.get_dtmf,
    )
    assert result == IvrExit.FREEFORM
    assert drv.prompts_played == 1
    assert drv.handlers_played == []


# ---------------------------------------------------------------------------
# Menu key → handler dispatch + loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_digit_dispatches_handler_then_loops() -> None:
    """Press 1, hear handler, return to menu, press 0 to drop."""
    drv = _Driver(scripted_dtmf=["1", "0"])
    ivr = IvrController(items=_menu_items(), drop_dtmf="0")
    result = await ivr.run(
        play_prompt=drv.play_prompt,
        play_handler_response=drv.play_handler_response,
        get_dtmf=drv.get_dtmf,
    )
    assert result == IvrExit.FREEFORM
    assert drv.handlers_played == ["house_status"]
    assert drv.prompts_played == 2  # initial + after handler


@pytest.mark.asyncio
async def test_multiple_handlers_then_drop() -> None:
    drv = _Driver(scripted_dtmf=["1", "2", "3", "4", "0"])
    ivr = IvrController(items=_menu_items(), drop_dtmf="0")
    result = await ivr.run(
        play_prompt=drv.play_prompt,
        play_handler_response=drv.play_handler_response,
        get_dtmf=drv.get_dtmf,
    )
    assert result == IvrExit.FREEFORM
    assert drv.handlers_played == ["house_status", "security_state", "door_locks", "doorbell_recent"]


@pytest.mark.asyncio
async def test_unknown_digit_replays_prompt_no_dispatch() -> None:
    """Pressing 9 (not in menu) should re-prompt without dispatching."""
    drv = _Driver(scripted_dtmf=["9", "1", "0"])
    ivr = IvrController(items=_menu_items(), drop_dtmf="0")
    result = await ivr.run(
        play_prompt=drv.play_prompt,
        play_handler_response=drv.play_handler_response,
        get_dtmf=drv.get_dtmf,
    )
    assert result == IvrExit.FREEFORM
    # Only valid handler dispatched
    assert drv.handlers_played == ["house_status"]
    # 9 → reprompt, then 1 → handler then re-prompt, then 0 → exit
    assert drv.prompts_played == 3


# ---------------------------------------------------------------------------
# Silence / timeout / hangup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_silence_max_reprompts_returns_hangup() -> None:
    drv = _Driver(scripted_dtmf=[None, None, None], scripted_timeouts=[True, True, True])
    ivr = IvrController(items=_menu_items(), drop_dtmf="0", max_reprompts=3)
    result = await ivr.run(
        play_prompt=drv.play_prompt,
        play_handler_response=drv.play_handler_response,
        get_dtmf=drv.get_dtmf,
    )
    assert result == IvrExit.HANGUP
    assert drv.prompts_played == 3
    assert drv.handlers_played == []


@pytest.mark.asyncio
async def test_silence_then_valid_resets_reprompt_counter() -> None:
    """Two silences, then press 1, then more silences should reset and reach max again."""
    # Sequence: silence, silence (reprompts=2), digit "1" (resets to 0),
    # then silence x3 to reach max again
    drv = _Driver(
        scripted_dtmf=[None, None, "1", None, None, None],
        scripted_timeouts=[True, True, False, True, True, True],
    )
    ivr = IvrController(items=_menu_items(), drop_dtmf="0", max_reprompts=3)
    result = await ivr.run(
        play_prompt=drv.play_prompt,
        play_handler_response=drv.play_handler_response,
        get_dtmf=drv.get_dtmf,
    )
    assert result == IvrExit.HANGUP
    assert drv.handlers_played == ["house_status"]


@pytest.mark.asyncio
async def test_handler_failure_does_not_break_loop() -> None:
    """If a handler raises, the IVR keeps looping."""
    drv = _Driver(scripted_dtmf=["1", "0"])

    async def crashing_handler(_name: str) -> None:
        raise RuntimeError("intentional")

    ivr = IvrController(items=_menu_items(), drop_dtmf="0")
    result = await ivr.run(
        play_prompt=drv.play_prompt,
        play_handler_response=crashing_handler,
        get_dtmf=drv.get_dtmf,
    )
    # Despite the crash, the loop returned to menu and processed the 0
    assert result == IvrExit.FREEFORM


@pytest.mark.asyncio
async def test_dtmf_source_exception_treated_as_timeout() -> None:
    """If get_dtmf raises, the IVR treats that cycle as silence."""
    crashes_left = [2]

    async def flaky_get_dtmf(_t: float):
        if crashes_left[0] > 0:
            crashes_left[0] -= 1
            raise OSError("transient")
        return "0"

    drv = _Driver(scripted_dtmf=[])
    ivr = IvrController(items=_menu_items(), drop_dtmf="0", max_reprompts=5)
    result = await ivr.run(
        play_prompt=drv.play_prompt,
        play_handler_response=drv.play_handler_response,
        get_dtmf=flaky_get_dtmf,
    )
    # Two crashes (silence), then "0" → FREEFORM
    assert result == IvrExit.FREEFORM
    assert drv.prompts_played == 3


# ---------------------------------------------------------------------------
# Custom configuration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_custom_drop_dtmf() -> None:
    drv = _Driver(scripted_dtmf=["#"])
    ivr = IvrController(items=_menu_items(), drop_dtmf="#")
    result = await ivr.run(
        play_prompt=drv.play_prompt,
        play_handler_response=drv.play_handler_response,
        get_dtmf=drv.get_dtmf,
    )
    assert result == IvrExit.FREEFORM


@pytest.mark.asyncio
async def test_empty_items_means_only_drop_works() -> None:
    """If there are no menu items, only the drop key produces an exit."""
    drv = _Driver(scripted_dtmf=["1", "2", "0"])
    ivr = IvrController(items=[], drop_dtmf="0")
    result = await ivr.run(
        play_prompt=drv.play_prompt,
        play_handler_response=drv.play_handler_response,
        get_dtmf=drv.get_dtmf,
    )
    assert result == IvrExit.FREEFORM
    assert drv.handlers_played == []
