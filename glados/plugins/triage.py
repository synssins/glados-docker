"""Plugin triage — bypass mode.

When `match_plugins` (keyword pre-filter) returns no plugins for a user
turn, this module decides which plugin tools the chat LLM should still
be allowed to see. The original Phase 2c design called the small
``llm_triage`` model for that decision; the live deployment on Intel
Arc Pro B60 / OVMS-on-Qwen3-30B couldn't keep that call under a chat-
budget timeout (~50% of turns stalled at 15 s and the chat path fell
through to chitchat with no plugin tools loaded — operator-flagged
2026-05-03 after the Spotify plugin shipped).

Bypass mode (this module): when triage is enabled, return the full
catalog of enabled plugin names. The chat LLM gets every plugin's
tools every turn that didn't keyword-match. Trades a small bump in
prompt tokens for reliable plugin reachability.

GLADOS_PLUGIN_TRIAGE_ENABLED still gates the call entirely:
* unset / "true" / "1" / "yes" / "on" (default) → return all plugin names
* "false" / "0" / "no" / "off" → return [] (chat sees no plugin tools)
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

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


def triage_plugins(
    message: str,
    plugins: list["Plugin"],
    timeout_s: float = 15.0,  # accepted for back-compat; unused in bypass mode
) -> list[str]:
    """Return the list of plugin names the chat LLM should see this turn.

    In bypass mode this is "all of them" whenever triage is on and the
    inputs are non-empty.

    Returns ``[]`` when:
    * ``GLADOS_PLUGIN_TRIAGE_ENABLED`` is falsy
    * ``plugins`` is empty
    * ``message`` is empty / whitespace
    """
    del timeout_s  # bypass mode does no LLM call; no budget to enforce.
    if not _enabled():
        _log_triage.debug("plugin triage: skipped (GLADOS_PLUGIN_TRIAGE_ENABLED falsy)")
        return []
    if not plugins or not message or not message.strip():
        return []
    names = [p.name for p in plugins]
    _log_triage.info(
        "plugin triage: bypass mode, advertising all {} enabled plugins: {}",
        len(names), names,
    )
    return names
