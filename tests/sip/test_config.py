"""Tests for glados.sip.config — schema, loader, env gate."""
from __future__ import annotations

import pathlib
import textwrap

import pytest
import yaml

from glados.sip.config import (
    SipConfig,
    SipIvrItem,
    SipServer,
    is_env_enabled,
    load_sip_config,
)


# ---------------------------------------------------------------------------
# is_env_enabled
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("true", True),
    ("True", True),
    ("TRUE", True),
    ("1", True),
    ("yes", True),
    ("on", True),
    ("false", False),
    ("0", False),
    ("no", False),
    ("off", False),
    ("", False),
    ("garbage", False),
])
def test_is_env_enabled(monkeypatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("GLADOS_SIP_ENABLED", raw)
    assert is_env_enabled() is expected


def test_is_env_enabled_unset(monkeypatch) -> None:
    monkeypatch.delenv("GLADOS_SIP_ENABLED", raising=False)
    assert is_env_enabled() is False


# ---------------------------------------------------------------------------
# SipServer secrets excluded from repr
# ---------------------------------------------------------------------------

def test_password_excluded_from_repr() -> None:
    s = SipServer(username="glados", password="hunter2")
    r = repr(s)
    assert "hunter2" not in r
    # Username is fine to show
    assert "glados" in r


# ---------------------------------------------------------------------------
# IVR validators
# ---------------------------------------------------------------------------

def test_ivr_key_must_be_dtmf_digit() -> None:
    SipIvrItem(key="1", label="Status", handler="status")
    SipIvrItem(key="*", label="Help", handler="help")
    SipIvrItem(key="#", label="Menu", handler="menu")
    with pytest.raises(ValueError, match="IVR key"):
        SipIvrItem(key="A", label="X", handler="x")
    with pytest.raises(ValueError, match="IVR key"):
        SipIvrItem(key="11", label="X", handler="x")


# ---------------------------------------------------------------------------
# Audio port range validator
# ---------------------------------------------------------------------------

def test_audio_port_range_validated() -> None:
    cfg_text = textwrap.dedent("""
        enabled: true
        server:
          username: glados
        audio:
          rtp_port_low: 20000
          rtp_port_high: 19000   # invalid: less than low
    """)
    with pytest.raises(ValueError, match="rtp_port_high"):
        SipConfig(**yaml.safe_load(cfg_text))


# ---------------------------------------------------------------------------
# load_sip_config
# ---------------------------------------------------------------------------

def _write_yaml(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    p = tmp_path / "sip.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_load_returns_none_if_missing(tmp_path: pathlib.Path) -> None:
    assert load_sip_config(tmp_path / "nonexistent.yaml") is None


def test_load_returns_none_if_disabled(tmp_path: pathlib.Path) -> None:
    p = _write_yaml(tmp_path, """
        enabled: false
        server:
          username: glados
    """)
    assert load_sip_config(p) is None


def test_load_returns_config_if_enabled(tmp_path: pathlib.Path) -> None:
    p = _write_yaml(tmp_path, """
        enabled: true
        server:
          host: 192.168.1.1
          username: glados
          password: secret
        inbound:
          pin: "8316"
          ivr_menu:
            enabled: true
            items:
              - key: "1"
                label: House
                handler: house_status
    """)
    cfg = load_sip_config(p)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.server.host == "192.168.1.1"
    assert cfg.server.username == "glados"
    assert cfg.server.password == "secret"
    assert cfg.inbound.pin == "8316"
    assert cfg.inbound.ivr_menu.enabled is True
    assert len(cfg.inbound.ivr_menu.items) == 1
    assert cfg.inbound.ivr_menu.items[0].key == "1"
    assert cfg.inbound.ivr_menu.items[0].handler == "house_status"


def test_load_raises_on_unknown_field(tmp_path: pathlib.Path) -> None:
    p = _write_yaml(tmp_path, """
        enabled: true
        server:
          username: glados
        bogus_root_field: value
    """)
    with pytest.raises(ValueError):
        load_sip_config(p)


def test_load_raises_on_missing_required_username(tmp_path: pathlib.Path) -> None:
    p = _write_yaml(tmp_path, """
        enabled: true
        server:
          host: 192.168.1.1
    """)
    with pytest.raises(ValueError):
        load_sip_config(p)


def test_load_with_env_dir(monkeypatch, tmp_path: pathlib.Path) -> None:
    """GLADOS_CONFIG_DIR resolution path."""
    monkeypatch.setenv("GLADOS_CONFIG_DIR", str(tmp_path))
    _write_yaml(tmp_path, """
        enabled: true
        server:
          username: glados
    """)
    cfg = load_sip_config()  # No path arg — uses env
    assert cfg is not None
    assert cfg.server.username == "glados"


def test_full_example_yaml_parses() -> None:
    """The shipped configs/sip.example.yaml must parse cleanly."""
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    example_path = repo_root / "configs" / "sip.example.yaml"
    assert example_path.exists(), f"missing {example_path}"

    with example_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # The example ships disabled (so load_sip_config would return None).
    # We bypass the gate to verify the schema parses.
    assert data is not None
    assert data["enabled"] is False
    # Force enabled=True for schema validation
    data["enabled"] = True
    cfg = SipConfig(**data)
    assert cfg.server.host == "192.168.1.1"
    assert len(cfg.inbound.ivr_menu.items) == 4
    assert cfg.inbound.ivr_menu.items[0].handler == "house_status"
    assert cfg.recordings.retention_count == 5
    assert "PCMU" in cfg.audio.codec_preference
    assert cfg.latency.speculative.enabled is True
    assert "pin_entry" in cfg.latency.speculative.branches


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_defaults_when_only_required_provided(tmp_path: pathlib.Path) -> None:
    p = _write_yaml(tmp_path, """
        enabled: true
        server:
          username: glados
    """)
    cfg = load_sip_config(p)
    assert cfg is not None
    # Defaults
    assert cfg.server.port == 5060
    assert cfg.server.transport == "UDP"
    assert cfg.inbound.pin_failures_max == 3
    assert cfg.inbound.recording_enabled is True
    assert cfg.outbound.enabled is False
    assert cfg.recordings.retention_count == 5
    assert cfg.audio.rtp_port_low == 16384
    assert cfg.audio.rtp_port_high == 16484
