"""mode: llm verdict -- triage-lane LLM decides act / don't-act.

Fail-safe direction is fixed: any timeout, error, or unparseable
response is a NO-ACT. Acting is never the failure default.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from loguru import logger

from glados.autonomy.llm_client import LLMConfig, llm_call
from glados.core.llm_directives import strip_thinking_response
from glados.events.config import EventRule

_SYSTEM_PROMPT = """\
You are the decision module of a smart-home event engine. An event has
fired and one pre-approved action is available. Decide whether to take
it, using ONLY the provided context.

Reply with ONLY a single JSON object, no prose, no markdown:
{"act": true|false, "reason": "one short sentence", "quip": "optional dry one-liner in GLaDOS's voice, empty string if none"}"""


@dataclass
class Verdict:
    act: bool
    reason: str
    quip: str = ""


def _no_act(why: str) -> Verdict:
    logger.warning("events decision error -> no-act: {}", why)
    return Verdict(act=False, reason=f"decision error: {why}")


def decide(
    rule: EventRule,
    get_state: Callable[[str], str | None],
    llm: Callable[..., str | None] = llm_call,
) -> Verdict:
    lines = [
        f"Question: {rule.decision_prompt}",
        f"Trigger: {rule.trigger.entity_id} changed to "
        f"'{rule.trigger.to_state}'.",
        f"Local time: {time.strftime('%A %H:%M')}",
        "Context:",
    ]
    for eid in rule.context_entities:
        state = get_state(eid)
        lines.append(f"  {eid}: {state if state is not None else 'unavailable'}")
    lines.append(
        f"Available action: {rule.action.target}. Should it run right now?"
    )
    user_prompt = "\n".join(lines)

    try:
        raw = llm(
            LLMConfig.for_slot("llm_triage"),
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=128,
        )
    except Exception as exc:
        return _no_act(f"LLM call raised: {exc}")
    if not raw:
        return _no_act("LLM returned empty response")

    txt = strip_thinking_response(raw)
    start = txt.find("{")
    end = txt.rfind("}")
    if start < 0 or end <= start:
        return _no_act(f"no JSON object in response: {txt[:80]!r}")
    try:
        obj = json.loads(txt[start:end + 1])
    except json.JSONDecodeError as exc:
        return _no_act(f"invalid JSON: {exc}")
    if not isinstance(obj, dict) or not isinstance(obj.get("act"), bool):
        return _no_act(f"missing/invalid 'act' field: {obj!r}")
    return Verdict(
        act=obj["act"],
        reason=str(obj.get("reason") or "").strip() or "(no reason given)",
        quip=str(obj.get("quip") or "").strip(),
    )
