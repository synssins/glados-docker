"""LLM-backed plugin intent triage.

Phase 2c gate #2: when the keyword pre-filter returns zero plugins,
ask the small fast triage model which plugins are relevant given the
user query and the catalog of (name, description) tuples. The slot
is resolved per ``services.yaml::llm_triage`` — currently
``OpenVINO/Qwen3-0.6B-int4-ov`` on the OpenArc instance at AIBox
(Change 41, 2026-05-03).

Why this matters: without triage, every keyword-miss turn dumps the
full plugin tool catalog at the chat LLM. Qwen3 with a crowded tool
catalog locks into "find a matching tool" mode and ignores the rest
of the system context — the operator hit this on
``"What time is it"`` returning *"The available tools do not include
a function to retrieve the current time"* despite the time-injection
context block being present (Change 39 + Change 40 bypass interaction).

10 s timeout -- triage runs INLINE on the chat path, so the budget
needs to stay tight. Why 10 s on a 0.6B classifier: OpenArc's
``OpenAIChatCompletionRequest`` model
(``src/server/models/requests_openai.py``) does NOT define a
``response_format`` field, so the schema-constrained decoding
``response_format: {"type": "json_schema", ...}`` we send is
silently dropped by pydantic. Without grammar enforcement Qwen3-0.6B
on CPU emits ~130-180 tokens of unconstrained output (some thinking
prefix + JSON answer) before stopping, which takes 5-7 s warm. 10 s
gives comfortable headroom for that and catches genuine stalls.
Optimization is captured as an open follow-up: either add
``response_format`` support to OpenArc upstream, or pass
``max_tokens`` to ``llm_call`` so the cap is enforced client-side.
Any failure (timeout, network error, malformed JSON, names outside
the enabled set) returns ``[]`` so the chitchat path falls back to
the existing no-tools behavior. Never raises.

Globally toggleable via ``GLADOS_PLUGIN_TRIAGE_ENABLED`` env. Default
``true``; any falsy value (``"false"``, ``"0"``, ``"no"``, ``"off"``)
disables the call entirely so deployments that don't want the extra
latency / model load can opt out without code changes.

History: Change 40 (2026-05-03 morning) bypassed the LLM call as a
workaround for OVMS Qwen3-30B's 11–25 s warm latency stalling
inline triage. Change 41 (same day, evening) replaced OVMS with
OpenArc and brought the small model online; this restores the
LLM-driven path against the 0.6B target.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

from loguru import logger

from glados.autonomy.llm_client import LLMConfig, llm_call
from glados.observability import LogGroupId, group_logger

_log_triage = group_logger(LogGroupId.PLUGIN.TRIAGE_LLM)

if TYPE_CHECKING:
    from .loader import Plugin


# Truthy gate matches the convention used by GLADOS_PLUGINS_ENABLED in
# engine._maybe_discover_plugin_configs.
_TRUTHY = {"1", "true", "yes", "on"}


def _enabled() -> bool:
    raw = os.environ.get("GLADOS_PLUGIN_TRIAGE_ENABLED", "true").strip().lower()
    return raw in _TRUTHY


_NONE_SENTINEL = "__none__"

_SYSTEM_PROMPT = (
    "/no_think\n\n"
    "You are a tool-routing classifier. Given a user message and a "
    "catalog of plugins, decide which plugin (if any) the user "
    "actually needs to satisfy the request. Reply with strict JSON: "
    '{"relevant": [<plugin_name>, ...]}.\n\n'
    "If — AND ONLY IF — the message is specifically about a plugin's "
    "domain, output that plugin's name verbatim. Otherwise output "
    f'``"{_NONE_SENTINEL}"`` as the single array element. Use '
    f"``{_NONE_SENTINEL}`` for weather questions, time-of-day "
    "queries, greetings, identity / capability questions, casual "
    "conversation, and any general chat. Do NOT list a real plugin "
    "'just in case'.\n\n"
    "Do not duplicate names. Do not invent names. Output one entry "
    "per relevant plugin, verbatim from the catalog.\n\n"
    "Examples:\n"
    "  User: 'Add a new movie to my library.'\n"
    "  Catalog: '- *arr Stack: Sonarr / Radarr / Lidarr / Prowlarr"
    " media management.'\n"
    '  Output: {"relevant": ["*arr Stack"]}\n\n'
    "  User: \"What's the weather?\"\n"
    "  Catalog: '- *arr Stack: Sonarr / Radarr / Lidarr / Prowlarr"
    " media management.'\n"
    f'  Output: {{"relevant": ["{_NONE_SENTINEL}"]}}\n\n'
    "  User: 'Who are you and what do you do?'\n"
    "  Catalog: '- *arr Stack: Sonarr / Radarr / Lidarr / Prowlarr"
    " media management.'\n"
    f'  Output: {{"relevant": ["{_NONE_SENTINEL}"]}}'
)


def _build_user_prompt(message: str, plugins: Iterable["Plugin"]) -> str:
    catalog_lines = []
    for p in plugins:
        desc = (p.manifest_v2.description or "").strip().replace("\n", " ")
        catalog_lines.append(f"- {p.name}: {desc}")
    catalog = "\n".join(catalog_lines) if catalog_lines else "(no plugins)"
    return (
        f"User message:\n{message.strip()}\n\n"
        f"Plugin catalog:\n{catalog}\n\n"
        'Reply with JSON: {"relevant": [...]}'
    )


def triage_plugins(
    message: str,
    plugins: list["Plugin"],
    timeout_s: float = 10.0,
) -> list[str]:
    """Ask the triage LLM which plugins are relevant for ``message``.

    Returns a list of plugin NAMES (matching ``plugin.name``) drawn
    from ``plugins``. Names returned by the LLM that aren't in the
    enabled set are filtered out -- the model occasionally hallucinates
    a plausible-looking name, and we don't want to advertise tools
    from a non-existent plugin.

    Returns ``[]`` when:
    * GLADOS_PLUGIN_TRIAGE_ENABLED is falsy
    * ``plugins`` is empty
    * ``message`` is empty / whitespace
    * The LLM call times out, errors, or returns unparseable content
    """
    if not _enabled():
        _log_triage.debug("plugin triage: skipped (GLADOS_PLUGIN_TRIAGE_ENABLED falsy)")
        return []
    if not plugins or not message or not message.strip():
        return []

    plugin_names = [p.name for p in plugins]
    _log_triage.info(
        "plugin triage: invoking llm_triage slot ({} plugins in catalog: {})",
        len(plugin_names), plugin_names,
    )
    # Schema-constrained decoding: enum the actual plugin names plus
    # an explicit ``__none__`` sentinel so the model has a grammar-
    # legal way to say "nothing applies". Without the sentinel the
    # 1B classifier kept committing to the only available enum value
    # for clearly unrelated queries (forecast, identity) — the
    # grammar made [] technically valid but the model didn't pick it.
    # Including the sentinel in the enum gives a token sequence the
    # model can deterministically commit to for the negative case.
    # The triage code drops the sentinel from ``matched`` so callers
    # only see real plugin names.
    triage_schema = {
        "name": "plugin_triage",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "relevant": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [*plugin_names, _NONE_SENTINEL],
                    },
                },
            },
            "required": ["relevant"],
            "additionalProperties": False,
        },
    }
    t0 = time.time()
    try:
        config = LLMConfig.for_slot("llm_triage", timeout=timeout_s)
        raw = llm_call(
            config,
            _SYSTEM_PROMPT,
            _build_user_prompt(message, plugins),
            json_schema=triage_schema,
            # Defensive cap — OpenArc doesn't honor json_schema, so the
            # model can ramble through 130-180 tokens of <think> at
            # default temperature regardless of /no_think. 256 leaves
            # room for typical think + JSON, catches runaway. Removing
            # the runaway is the visible win; the typical-case latency
            # is bounded by the model's natural decode time, not this.
            max_tokens=256,
        )
    except Exception as exc:  # noqa: BLE001
        _log_triage.warning(
            "plugin triage: LLM call raised after {:.0f}ms: {}",
            (time.time() - t0) * 1000, exc,
        )
        return []

    elapsed_ms = (time.time() - t0) * 1000
    if not raw:
        _log_triage.info("plugin triage: empty response after {:.0f}ms", elapsed_ms)
        return []

    _log_triage.info(
        "plugin triage: response in {:.0f}ms, raw[:200]={!r}",
        elapsed_ms, raw[:200],
    )
    _log_triage.debug("plugin triage: full raw response: {!r}", raw)

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        _log_triage.warning("plugin triage: JSON parse failed ({}); raw={!r}", exc, raw[:200])
        return []

    relevant = parsed.get("relevant") if isinstance(parsed, dict) else None
    if not isinstance(relevant, list):
        _log_triage.info("plugin triage: parsed object missing 'relevant' list; parsed={!r}", parsed)
        return []

    enabled_names = {p.name for p in plugins}
    # Drop the ``__none__`` sentinel (the grammar-legal "nothing
    # applies" marker), then dedup while preserving first-seen order.
    # Schema-constrained decoding on a small classifier model
    # occasionally pads the array with duplicates when the catalog
    # has only one valid enum value — collapsing here keeps the
    # multiplicity honest downstream.
    seen: set[str] = set()
    matched: list[str] = []
    for n in relevant:
        if not isinstance(n, str) or n == _NONE_SENTINEL:
            continue
        if n in enabled_names and n not in seen:
            matched.append(n)
            seen.add(n)
    dropped = [
        n for n in relevant
        if isinstance(n, str) and n != _NONE_SENTINEL and n not in enabled_names
    ]
    if dropped:
        _log_triage.warning(
            "plugin triage: dropped hallucinated names not in enabled set: {}",
            dropped,
        )
    _log_triage.info("plugin triage: matched={}", matched)
    return matched
