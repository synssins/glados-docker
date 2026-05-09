"""SIP config loader — reads ``configs/sip.yaml`` into typed Pydantic models.

Two gates govern whether the SIP feature is active:

1. ``GLADOS_SIP_ENABLED`` env var (truthy = let the module load at all).
   ``is_env_enabled()`` is the cheap pre-check; callers should run this
   before doing anything else SIP-related.
2. ``configs/sip.yaml`` ``enabled: true`` (truthy = register with PBX
   and accept calls). ``load_sip_config()`` returns ``None`` when this
   is false OR when the file is missing.

Both gates must agree before the SIP subsystem starts.

Secrets (PIN, SIP password) are stored in the bind-mounted YAML —
deliberately NOT in the image, NOT in env vars, NOT in git. The host
filesystem is the operator's defended perimeter. ``__repr__`` excludes
the PIN and password fields so log lines don't leak them.
"""
from __future__ import annotations

import os
import pathlib
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Env gate
# ---------------------------------------------------------------------------

def is_env_enabled() -> bool:
    """True iff GLADOS_SIP_ENABLED is set to a truthy value."""
    raw = os.environ.get("GLADOS_SIP_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Server / connection
# ---------------------------------------------------------------------------

class SipServer(BaseModel):
    """SIP registrar / proxy connection details."""
    model_config = ConfigDict(extra="forbid")

    host: str = "192.168.1.1"
    port: int = 5060
    username: str
    password: str = Field("", repr=False)  # Excluded from repr — secret
    transport: Literal["UDP", "TCP", "TLS"] = "UDP"
    realm: str = ""
    register_expires: int = 600


# ---------------------------------------------------------------------------
# Inbound (Slice 1)
# ---------------------------------------------------------------------------

class SipIvrItem(BaseModel):
    """One menu entry in the post-PIN IVR."""
    model_config = ConfigDict(extra="forbid")

    key: str             # DTMF digit, "0".."9", "*", "#"
    label: str           # Human-readable for the prompt
    handler: str         # Name of the handler module (e.g. "house_status")

    @field_validator("key")
    @classmethod
    def _validate_key(cls, v: str) -> str:
        if v not in set("0123456789*#"):
            raise ValueError(f"IVR key must be 0-9, *, or # (got {v!r})")
        return v


class SipIvrMenu(BaseModel):
    """DTMF-driven menu shown to authenticated callers post-PIN."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    drop_to_freeform_dtmf: str = "0"
    items: list[SipIvrItem] = Field(default_factory=list)

    @field_validator("drop_to_freeform_dtmf")
    @classmethod
    def _validate_drop(cls, v: str) -> str:
        if v not in set("0123456789*#"):
            raise ValueError(f"drop_to_freeform_dtmf must be a single DTMF digit (got {v!r})")
        return v


class SipInbound(BaseModel):
    """Settings for inbound calls."""
    model_config = ConfigDict(extra="forbid")

    pin: str = Field("", repr=False)  # Excluded from repr — secret
    pin_failures_max: int = 3
    greeting_template: str = "default"
    recording_enabled: bool = True
    allow_caller_ids: list[str] = Field(default_factory=list)
    ivr_menu: SipIvrMenu = Field(default_factory=SipIvrMenu)


# ---------------------------------------------------------------------------
# Outbound + autonomous (Slices 2/3 — schema reserved, no logic yet)
# ---------------------------------------------------------------------------

class SipContact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    number: str  # E.164 format expected


class SipOutbound(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    contacts: list[SipContact] = Field(default_factory=list)


class SipAutonomousAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    target_contact: str = ""
    cooldown_seconds: int = 300
    schedule_cron: str = ""  # Only used by the "scheduled" alert


class SipAutonomous(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doorbell: SipAutonomousAlert = Field(default_factory=SipAutonomousAlert)
    fire_alarm: SipAutonomousAlert = Field(default_factory=SipAutonomousAlert)
    scheduled: SipAutonomousAlert = Field(default_factory=SipAutonomousAlert)


# ---------------------------------------------------------------------------
# Recordings, audio, latency
# ---------------------------------------------------------------------------

class SipRecordings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    retention_count: int = 5
    store_path: str = "media/sip-recordings"
    format: Literal["mp3", "wav"] = "mp3"


class SipAudio(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rtp_port_low: int = 16384
    rtp_port_high: int = 16484
    codec_preference: list[str] = Field(default_factory=lambda: ["PCMU", "PCMA", "G722"])
    vad_silence_ms: int = 800

    @field_validator("rtp_port_high")
    @classmethod
    def _validate_port_range(cls, v: int, info) -> int:
        low = info.data.get("rtp_port_low", 16384)
        if v < low:
            raise ValueError(f"rtp_port_high ({v}) must be >= rtp_port_low ({low})")
        return v


class SipLatencySpeculative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    max_concurrent: int = 4
    branches: dict[str, list[str]] = Field(default_factory=dict)


class SipLatency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filler_phrases: list[str] = Field(default_factory=list)
    filler_threshold_ms: int = 1500
    use_autonomy_model: bool = False
    speculative: SipLatencySpeculative = Field(default_factory=SipLatencySpeculative)


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------

class SipConfig(BaseModel):
    """Top-level SIP config matching ``configs/sip.yaml``."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    server: SipServer
    inbound: SipInbound = Field(default_factory=SipInbound)
    outbound: SipOutbound = Field(default_factory=SipOutbound)
    autonomous: SipAutonomous = Field(default_factory=SipAutonomous)
    recordings: SipRecordings = Field(default_factory=SipRecordings)
    audio: SipAudio = Field(default_factory=SipAudio)
    latency: SipLatency = Field(default_factory=SipLatency)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_sip_config(path: pathlib.Path | str | None = None) -> SipConfig | None:
    """Load ``configs/sip.yaml``. Returns ``None`` if disabled or missing.

    Resolution order for the path:
    1. The ``path`` argument if provided.
    2. ``$GLADOS_CONFIG_DIR/sip.yaml`` if the env var is set.
    3. ``configs/sip.yaml`` relative to the current working directory.

    Returns ``None`` when:
    - The file does not exist, OR
    - The loaded config has ``enabled: false``.

    Raises ``ValueError`` (wrapping pydantic.ValidationError) when the
    YAML schema is invalid — operator should see a clear message and
    fix the file rather than have us silently start with broken
    defaults.
    """
    if path is None:
        cfg_dir = os.environ.get("GLADOS_CONFIG_DIR", "configs")
        path = pathlib.Path(cfg_dir) / "sip.yaml"
    else:
        path = pathlib.Path(path)

    if not path.exists():
        return None

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    cfg = SipConfig(**data)
    return cfg if cfg.enabled else None
