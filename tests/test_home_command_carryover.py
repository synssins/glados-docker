"""P0 2026-04-19 — follow-up turns without a device keyword bypass
Tier 1/2 entirely.

Operator report: after GLaDOS had just acted on the desk lamp, "It's
still too dark. Turn it up more." had no 'light' / 'lamp' / 'turn on'
keyword → `looks_like_home_command` returned False → api_wrapper
skipped Tier 1 and Tier 2 → Tier 3 chitchat hallucinated a status
confirmation without calling a tool.

Fix spans two layers:

  1. `_should_carry_over_home_command` consults an in-memory
     recent-action cache so a follow-up turn can inherit home-command
     intent for the next 5 minutes.
  2. The recent-action cache also carries the prior turn's
     `entity_ids` and `service`, which get threaded into the
     disambiguator so it can act on the implied target even when the
     current utterance fuzzy-matches nothing.

This file covers layer 1 in isolation; disambiguator-layer coverage
for the prior-entity injection lives in tests/test_disambiguator.py.
"""

from __future__ import annotations

import time

import pytest


class TestShouldCarryOverHomeCommand:
    def setup_method(self) -> None:
        from glados.core import api_wrapper
        with api_wrapper._RECENT_TIER_LOCK:
            api_wrapper._RECENT_TIER_ACTION.clear()

    def test_empty_cache_returns_false(self) -> None:
        from glados.core import api_wrapper
        assert api_wrapper._should_carry_over_home_command() is False

    def test_recent_tier2_returns_true(self) -> None:
        from glados.core import api_wrapper
        api_wrapper._stash_recent_tier_action(
            "default", tier=2,
            entity_ids=["light.task_lamp_one"],
            service="light.turn_on",
        )
        assert api_wrapper._should_carry_over_home_command() is True

    def test_recent_tier1_returns_true(self) -> None:
        from glados.core import api_wrapper
        api_wrapper._stash_recent_tier_action(
            "default", tier=1,
            entity_ids=["light.kitchen"],
            service="",
        )
        assert api_wrapper._should_carry_over_home_command() is True

    def test_cleared_cache_returns_false(self) -> None:
        """Intervening Tier 3 chitchat cancels the lease."""
        from glados.core import api_wrapper
        api_wrapper._stash_recent_tier_action(
            "default", tier=2, entity_ids=["light.x"],
        )
        api_wrapper._clear_recent_tier_action()
        assert api_wrapper._should_carry_over_home_command() is False

    def test_stale_window_returns_false(self, monkeypatch) -> None:
        from glados.core import api_wrapper
        api_wrapper._stash_recent_tier_action(
            "default", tier=2, entity_ids=["light.x"],
        )
        # Shrink the window to zero and wait a beat. The cache entry
        # is older than the budget → returns None/False.
        monkeypatch.setattr(
            api_wrapper, "_FOLLOWUP_HOME_COMMAND_WINDOW_S", 0.01,
        )
        time.sleep(0.02)
        assert api_wrapper._should_carry_over_home_command() is False

    def test_get_recent_returns_full_record(self) -> None:
        from glados.core import api_wrapper
        api_wrapper._stash_recent_tier_action(
            "default", tier=2,
            entity_ids=["light.a", "light.b"],
            service="light.turn_on",
            ha_conversation_id="ha-99",
        )
        rec = api_wrapper._get_recent_tier_action()
        assert rec is not None
        assert rec.tier == 2
        assert rec.entity_ids == ["light.a", "light.b"]
        assert rec.service == "light.turn_on"
        assert rec.ha_conversation_id == "ha-99"

    def test_default_window_is_five_minutes(self) -> None:
        """Default window should be long enough for natural follow-up
        rhythms (the operator's 'Increase the brightness by ten
        percent' follow-up was ~4 minutes after the prior turn)."""
        from glados.core import api_wrapper
        assert api_wrapper._FOLLOWUP_HOME_COMMAND_WINDOW_S >= 300
