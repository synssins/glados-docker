"""
Pydantic models for ``configs/robots.yaml``.

Integrated into :class:`glados.core.config_store.GladosConfigStore`
alongside the other config sections.
"""

from __future__ import annotations

from pydantic import BaseModel


class RobotNodeConfig(BaseModel):
    """Configuration for a single ESP32 robot node."""

    url: str                                # e.g. "http://robot.local"
    enabled: bool = True
    name: str = ""                          # Human-readable (auto-populated from node if blank)
    token: str = ""                         # Per-node auth override (falls back to global auth_token)
    capabilities: list[str] = []            # e.g. ["servo", "led", "sensor"] — auto-discovered


class BotConfig(BaseModel):
    """Configuration for a composite bot (collection of nodes)."""

    profile: str = "custom"                 # Profile: arm | tracked | 4wheel | arm_on_base | custom
    enabled: bool = True
    name: str = ""                          # Human-readable bot name
    nodes: dict[str, str] = {}              # role -> node_id mapping (e.g. {"arm": "arm_node"})


class RobotsConfig(BaseModel):
    """Top-level ``robots.yaml`` schema."""

    enabled: bool = False
    health_poll_interval_s: float = 15.0
    request_timeout_s: float = 5.0
    emergency_stop_timeout_s: float = 2.0
    auth_token: str = ""                    # Global Bearer token (per-node overrides in RobotNodeConfig)
    nodes: dict[str, RobotNodeConfig] = {}  # node_id -> config
    bots: dict[str, BotConfig] = {}         # bot_id -> config
