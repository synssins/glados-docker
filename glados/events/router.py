"""EventRouter -- match HA state changes against operator rules and act.

Gate order (spec sec 4): master enabled -> quiet/maintenance -> rule.enabled
-> cooldown (consumed by declines too) -> min_clear. Every fire, decline,
and failure is audited and kept in per-rule runtime status.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from loguru import logger

from glados.events.actions.ha_action import HAActionError, execute_ha_action
from glados.events.announce import announce as default_announce
from glados.events.config import (
    EventRule,
    EventsConfig,
    EventsConfigError,
    load_events_config,
)
from glados.events.decision import Verdict, decide as default_decide
from glados.observability.audit import AuditEvent, Origin, audit


class EventRouter:
    def __init__(
        self,
        config_path: Path,
        call_service: Callable[..., dict],
        get_state: Callable[[str], str | None],
        decision_fn: Callable[..., Verdict] = default_decide,
        action_fn: Callable[..., None] = execute_ha_action,
        announce_fn: Callable[..., bool] = default_announce,
        quiet_check: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config_path = config_path
        self._call_service = call_service
        self._get_state = get_state
        self._decision_fn = decision_fn
        self._action_fn = action_fn
        self._announce_fn = announce_fn
        self._quiet_check = quiet_check
        self._clock = clock
        self._lock = threading.Lock()
        self._config = EventsConfig(enabled=False, rules=[])
        self._load_error: str | None = None
        # Per-rule runtime state.
        self._last_attempt: dict[str, float] = {}   # fire OR decline ts
        self._clear_since: dict[str, float] = {}    # entity left trigger state
        self._status: dict[str, dict] = {}
        self._fire_count: dict[str, int] = {}

    # -- config lifecycle -------------------------------------------------

    def load(self) -> bool:
        """(Re)read events.yaml. On schema error: engine off, loud log."""
        try:
            with self._lock:
                self._config = load_events_config(self._config_path)
                self._load_error = None
            logger.success(
                "events: loaded {} rule(s), engine {}",
                len(self._config.rules),
                "enabled" if self._config.enabled else "DISABLED",
            )
            return True
        except EventsConfigError as exc:
            with self._lock:
                self._config = EventsConfig(enabled=False, rules=[])
                self._load_error = str(exc)
            logger.error("events: config error -- engine disabled: {}", exc)
            return False

    # -- event handling ---------------------------------------------------

    def handle_state_changed(self, data: dict) -> None:
        entity_id = data.get("entity_id") or ""
        new = (data.get("new_state") or {}).get("state")
        old = (data.get("old_state") or {}).get("state")
        with self._lock:
            cfg = self._config
        for rule in cfg.rules:
            if rule.trigger.entity_id != entity_id:
                continue
            # Track when the entity LEFT the trigger state (for min_clear).
            if old == rule.trigger.to_state and new != rule.trigger.to_state:
                self._clear_since[rule.id] = self._clock()
                continue
            if new != rule.trigger.to_state:
                continue
            if rule.trigger.from_state is not None and old != rule.trigger.from_state:
                continue
            self._gate_and_run(rule, cfg)

    def _gate_and_run(self, rule: EventRule, cfg: EventsConfig) -> None:
        now = self._clock()
        if not cfg.enabled:
            return
        if self._quiet_check():
            logger.success("events: {} suppressed (quiet/maintenance mode)", rule.id)
            return
        if not rule.enabled:
            return
        last = self._last_attempt.get(rule.id, 0.0)
        if last and now - last < rule.cooldown_s:
            return
        if rule.min_clear_s > 0:
            cleared = self._clear_since.get(rule.id)
            if cleared is not None and now - cleared < rule.min_clear_s:
                return
        self._last_attempt[rule.id] = now
        self._execute(rule, manual=False)

    # -- execution (shared by live events and the Fire button) -----------

    def _execute(self, rule: EventRule, *, manual: bool) -> dict:
        verdict = Verdict(act=True, reason="mode: always")
        if rule.mode == "llm":
            verdict = self._decision_fn(rule, self._get_state)
        if not verdict.act:
            self._record(rule, "declined", verdict.reason)
            return {"result": "declined", "reason": verdict.reason}
        try:
            self._action_fn(rule.action, self._call_service)
        except HAActionError as exc:
            self._record(rule, "error", str(exc))
            return {"result": "error", "reason": str(exc)}
        self._fire_count[rule.id] = self._fire_count.get(rule.id, 0) + 1
        self._record(rule, "fired", verdict.reason, manual=manual)
        if rule.announce and rule.announce_speaker:
            text = verdict.quip if rule.mode == "llm" else (rule.announce_text or "")
            if text:
                self._announce_fn(text, rule.announce_speaker, self._call_service)
        return {"result": "fired", "reason": verdict.reason}

    def _record(self, rule: EventRule, result: str, reason: str, *, manual: bool = False) -> None:
        self._status[rule.id] = {
            "last_result": result,
            "last_reason": reason,
            "last_ts": self._clock(),
        }
        level = logger.error if result == "error" else logger.success
        level("events: rule {} -> {} ({}){}", rule.id, result, reason,
              " [manual]" if manual else "")
        try:
            audit(AuditEvent(
                ts=self._clock(),
                origin=Origin.EVENT_RULE,
                kind=f"event_{result}",
                extra={"detail": f"{rule.id}: {reason}"},
            ))
        except Exception:           # audit must never break dispatch
            logger.warning("events: audit emit failed for rule {}", rule.id)

    # -- WebUI surface ----------------------------------------------------

    def run_rule(self, rule_id: str, *, dry_run: bool) -> dict:
        """Dry-run (decision only) or manual Fire (bypasses gates --
        operator-initiated, so the unattended-action rule is satisfied)."""
        with self._lock:
            rule = next((r for r in self._config.rules if r.id == rule_id), None)
        if rule is None:
            return {"error": f"no rule {rule_id!r}"}
        if dry_run:
            verdict = (
                self._decision_fn(rule, self._get_state)
                if rule.mode == "llm"
                else Verdict(act=True, reason="mode: always")
            )
            return {"verdict": {"act": verdict.act, "reason": verdict.reason,
                                "quip": verdict.quip}}
        return self._execute(rule, manual=True)

    def status(self) -> dict:
        with self._lock:
            cfg = self._config
            err = self._load_error
        return {
            "enabled": cfg.enabled,
            "load_error": err,
            "rules": [
                {
                    **r.model_dump(),
                    **self._status.get(r.id, {"last_result": "never",
                                              "last_reason": "", "last_ts": 0.0}),
                    "fire_count": self._fire_count.get(r.id, 0),
                }
                for r in cfg.rules
            ],
        }
