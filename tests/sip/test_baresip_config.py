"""Tests for glados.sip._baresip_config — config + accounts file rendering."""
from __future__ import annotations

import pathlib

import pytest
import yaml

from glados.sip._baresip_config import (
    _REQUIRED_MODULES,
    render_accounts,
    render_config,
    write_baresip_files,
)
from glados.sip.config import SipConfig


def _minimal_cfg(**overrides) -> SipConfig:
    """Build a minimal SipConfig for tests."""
    base = {
        "enabled": True,
        "server": {
            "host": "192.168.1.1",
            "port": 5060,
            "username": "glados",
            "password": "secret",
        },
    }
    base.update(overrides)
    return SipConfig(**base)


# ---------------------------------------------------------------------------
# render_config
# ---------------------------------------------------------------------------

def test_config_lists_all_required_modules() -> None:
    body = render_config(_minimal_cfg())
    for mod in _REQUIRED_MODULES:
        assert f"module {mod}" in body, f"missing required module {mod}"


def test_config_uses_aufile_for_audio_with_provided_fifo_paths() -> None:
    body = render_config(_minimal_cfg(),
                          rx_fifo="/var/run/sip-rx",
                          tx_fifo="/var/run/sip-tx")
    assert "audio_player aufile,/var/run/sip-tx" in body
    assert "audio_source aufile,/var/run/sip-rx" in body


def test_config_includes_rtp_port_range() -> None:
    cfg = SipConfig(
        enabled=True,
        server={"username": "glados"},
        audio={"rtp_port_low": 20000, "rtp_port_high": 20100},
    )
    body = render_config(cfg)
    assert "rtp_ports 20000-20100" in body


def test_config_includes_codec_preferences() -> None:
    cfg = SipConfig(
        enabled=True,
        server={"username": "glados"},
        audio={"codec_preference": ["G722", "PCMU"]},
    )
    body = render_config(cfg)
    assert "audio_codecs G722,PCMU" in body


def test_config_ctrl_tcp_listen_on_loopback() -> None:
    body = render_config(_minimal_cfg(), ctrl_tcp_port=4444)
    assert "ctrl_tcp_listen 127.0.0.1:4444" in body


def test_config_no_gui_modules() -> None:
    """Slice 1 deliberately excludes GUI/X11 modules."""
    body = render_config(_minimal_cfg())
    for forbidden in ("module gtk.so", "module x11.so", "module gst.so",
                      "module gst1.so", "module ffmpeg.so"):
        assert forbidden not in body, f"unexpected module {forbidden}"


# ---------------------------------------------------------------------------
# render_accounts
# ---------------------------------------------------------------------------

def test_accounts_includes_aor_and_password() -> None:
    body = render_accounts(_minimal_cfg())
    assert "<sip:glados@192.168.1.1>" in body
    assert "auth_pass=secret" in body
    assert "regint=600" in body
    assert "transport=udp" in body


def test_accounts_omits_password_when_empty() -> None:
    cfg = SipConfig(
        enabled=True,
        server={"host": "192.168.1.1", "username": "glados", "password": ""},
    )
    body = render_accounts(cfg)
    assert "auth_pass=" not in body


def test_accounts_uses_register_expires_value() -> None:
    cfg = SipConfig(
        enabled=True,
        server={"username": "glados", "register_expires": 1200},
    )
    body = render_accounts(cfg)
    assert "regint=1200" in body


def test_accounts_realm_sets_auth_user() -> None:
    cfg = SipConfig(
        enabled=True,
        server={"username": "glados", "password": "x", "realm": "pbx.local"},
    )
    body = render_accounts(cfg)
    assert "auth_user=glados" in body


# ---------------------------------------------------------------------------
# write_baresip_files
# ---------------------------------------------------------------------------

def test_write_files_creates_directory(tmp_path: pathlib.Path) -> None:
    outdir = tmp_path / "baresip"
    write_baresip_files(_minimal_cfg(), outdir)
    assert (outdir / "config").exists()
    assert (outdir / "accounts").exists()


def test_write_files_idempotent(tmp_path: pathlib.Path) -> None:
    outdir = tmp_path / "baresip"
    write_baresip_files(_minimal_cfg(), outdir)
    write_baresip_files(_minimal_cfg(), outdir)
    # Second call shouldn't raise; files still there
    assert (outdir / "config").exists()


def test_write_files_forwards_kwargs(tmp_path: pathlib.Path) -> None:
    outdir = tmp_path / "baresip"
    write_baresip_files(_minimal_cfg(), outdir, ctrl_tcp_port=9999)
    body = (outdir / "config").read_text()
    assert "ctrl_tcp_listen 127.0.0.1:9999" in body
