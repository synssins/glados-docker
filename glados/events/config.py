"""Schema + load/save for configs/events.yaml (single source of truth)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class EventsConfigError(Exception):
    """events.yaml is unreadable or fails schema validation."""


class EventTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str
    to_state: str
    from_state: str | None = None


_KIND_PREFIX = {
    "ha_automation": "automation.",
    "ha_script": "script.",
    "ha_scene": "scene.",
}


class EventActionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["ha_automation", "ha_script", "ha_scene", "ha_service"]
    target: str
    entity_id: str | None = None          # ha_service only
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_target(self) -> "EventActionSpec":
        prefix = _KIND_PREFIX.get(self.kind)
        if prefix and not self.target.startswith(prefix):
            raise ValueError(
                f"action target for kind={self.kind} must start with {prefix!r}"
            )
        if self.kind == "ha_service" and "." not in self.target:
            raise ValueError("ha_service target must be 'domain.service'")
        return self


class EventRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    enabled: bool = False
    trigger: EventTrigger
    mode: Literal["always", "llm"] = "always"
    context_entities: list[str] = Field(default_factory=list)
    decision_prompt: str | None = None
    action: EventActionSpec
    cooldown_s: float = 60.0
    min_clear_s: float = 0.0
    announce: bool = False
    announce_speaker: str | None = None
    announce_text: str | None = None

    @model_validator(mode="after")
    def _check_conditionals(self) -> "EventRule":
        if self.mode == "llm" and not (self.decision_prompt or "").strip():
            raise ValueError(f"rule {self.id!r}: mode=llm requires decision_prompt")
        if self.announce and not self.announce_speaker:
            raise ValueError(f"rule {self.id!r}: announce=true requires announce_speaker")
        if self.announce and self.mode == "always" and not (self.announce_text or "").strip():
            raise ValueError(
                f"rule {self.id!r}: announce=true with mode=always requires announce_text"
            )
        return self


class EventsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    rules: list[EventRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "EventsConfig":
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        return self


def default_events_path() -> Path:
    return Path(os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")) / "events.yaml"


def load_events_config(path: Path) -> EventsConfig:
    """Missing file -> defaults (engine on, no rules). Malformed -> raise."""
    if not path.exists():
        return EventsConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return EventsConfig.model_validate(raw)
    except Exception as exc:
        raise EventsConfigError(f"{path}: {exc}") from exc


def save_events_config(path: Path, config: EventsConfig) -> None:
    """Atomic write: temp file + os.replace, mirroring config-store style."""
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(config.model_dump(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)
