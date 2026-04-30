"""LLM-backed plugin intent triage.

Phase 2c gate #2: when the keyword pre-filter returns zero plugins,
ask the small fast triage model (default ``llama-3.2-1b-instruct``,
~300-500 ms warm) which plugins are relevant given the user query
and the catalog of (name, description) tuples.

Strict 1.5 s timeout -- this runs INLINE on the chat path, so the
budget has to stay tight or the operator notices. Any failure
(timeout, network error, malformed JSON, names outside the enabled
set) returns ``[]`` so the chitchat path falls back to the existing
no-tools behavior. Never raises.

Globally toggleable via ``GLADOS_PLUGIN_TRIAGE_ENABLED`` env. Default
``true``; any falsy value (``"false"``, ``"0"``, ``"no"``, ``"off"``)
disables the call entirely so deployments that don't want the extra
latency / model load can opt out without code changes.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

from loguru import logger

from glados.autonomy.llm_client import LLMConfig, llm_call

if TYPE_CHECKING:
    from .loader import Plugin


# Truthy gate matches the convention used by GLADOS_PLUGINS_ENABLED in
# engine._maybe_discover_plugin_configs.
_TRUTHY = {"1", "true", "yes", "on"}


def _enabled() -> bool:
    raw = os.environ.get("GLADOS_PLUGIN_TRIAGE_ENABLED", "true").strip().lower()
    return raw in _TRUTHY


_SYSTEM_PROMPT = (
    "You are a tool-routing classifier. Given a user message and a "
    "catalog of plugins (name + description), return ONLY the JSON "
    'object {"relevant": [<name>, ...]} listing every plugin whose '
    "tools the user might need to satisfy the request. Return an "
    "empty list if no plugin is relevant. Do not invent names; copy "
    "them verbatim from the catalog."
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
    timeout_s: float = 1.5,
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
        logger.success("plugin triage: skipped (GLADOS_PLUGIN_TRIAGE_ENABLED falsy)")
        return []
    if not plugins or not message or not message.strip():
        return []

    plugin_names = [p.name for p in plugins]
    logger.success(
        "plugin triage: invoking llm_triage slot ({} plugins in catalog: {})",
        len(plugin_names), plugin_names,
    )
    t0 = time.time()
    try:
        config = LLMConfig.for_slot("llm_triage", timeout=timeout_s)
        # Determinism matters here; the temperature kwarg isn't
        # exposed on llm_call, but the underlying triage model is
        # already tuned hot toward classification by the slot
        # default, and the JSON-response path constrains output.
        raw = llm_call(
            config,
            _SYSTEM_PROMPT,
            _build_user_prompt(message, plugins),
            json_response=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "plugin triage: LLM call raised after {:.0f}ms: {}",
            (time.time() - t0) * 1000, exc,
        )
        return []

    elapsed_ms = (time.time() - t0) * 1000
    if not raw:
        logger.success("plugin triage: empty response after {:.0f}ms", elapsed_ms)
        return []

    logger.success(
        "plugin triage: response in {:.0f}ms, raw[:200]={!r}",
        elapsed_ms, raw[:200],
    )

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("plugin triage: JSON parse failed ({}); raw={!r}", exc, raw[:200])
        return []

    relevant = parsed.get("relevant") if isinstance(parsed, dict) else None
    if not isinstance(relevant, list):
        logger.success("plugin triage: parsed object missing 'relevant' list; parsed={!r}", parsed)
        return []

    enabled_names = {p.name for p in plugins}
    matched = [n for n in relevant if isinstance(n, str) and n in enabled_names]
    dropped = [n for n in relevant if isinstance(n, str) and n not in enabled_names]
    if dropped:
        logger.warning(
            "plugin triage: dropped hallucinated names not in enabled set: {}",
            dropped,
        )
    logger.success("plugin triage: matched={}", matched)
    return matched
