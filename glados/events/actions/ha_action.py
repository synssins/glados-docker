"""Execute a whitelisted HA action via HAClient.call_service.

One retry with a short backoff, then a loud, typed failure -- never a
silent one (house rule).
"""
from __future__ import annotations

import time
from typing import Callable

from loguru import logger

from glados.events.config import EventActionSpec


class HAActionError(Exception):
    """The HA service call failed after retry."""


def _map_call(spec: EventActionSpec) -> tuple[str, str, dict, dict]:
    if spec.kind == "ha_automation":
        return "automation", "trigger", {}, {"entity_id": spec.target}
    if spec.kind == "ha_script":
        return "script", "turn_on", {}, {"entity_id": spec.target}
    if spec.kind == "ha_scene":
        return "scene", "turn_on", {}, {"entity_id": spec.target}
    domain, service = spec.target.split(".", 1)
    target = {"entity_id": spec.entity_id} if spec.entity_id else {}
    return domain, service, dict(spec.data), target


def execute_ha_action(
    spec: EventActionSpec,
    call_service: Callable[..., dict],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    domain, service, service_data, target = _map_call(spec)
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            call_service(domain, service, service_data=service_data, target=target)
            if attempt == 2:
                logger.warning("events ha_action {} succeeded on retry", spec.target)
            return
        except Exception as exc:
            last_exc = exc
            logger.error(
                "events ha_action {}/{} target={} attempt {} failed: {}",
                domain, service, spec.target, attempt, exc,
            )
            if attempt == 1:
                sleep(1.0)
    raise HAActionError(f"{domain}.{service} ({spec.target}) failed twice: {last_exc}")
