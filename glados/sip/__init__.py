"""SIP client integration — GLaDOS as a phone endpoint.

baresip subprocess inside the container handles SIP signalling and RTP.
This Python package controls baresip via its `ctrl_tcp` JSON interface
on loopback and bridges PCM audio via named pipes (FIFOs).

See:
- docs/superpowers/specs/2026-05-08-sip-client-design.md  (architecture)
- docs/superpowers/plans/2026-05-08-sip-slice-1-inbound.md (Slice 1 task plan)

Slice 1 (inbound) is shipped here. Slices 2 (outbound + WebUI page) and
3 (autonomous alerts) are deferred — see the spec for phasing.

Two gates:
1. ``GLADOS_SIP_ENABLED`` env var — module never loads when false
2. ``configs/sip.yaml`` ``enabled: true`` — module never registers when false
"""
from glados.sip.config import (
    SipAudio,
    SipConfig,
    SipInbound,
    SipIvrItem,
    SipIvrMenu,
    SipLatency,
    SipLatencySpeculative,
    SipRecordings,
    SipServer,
    is_env_enabled,
    load_sip_config,
)

__all__ = [
    "SipAudio",
    "SipConfig",
    "SipInbound",
    "SipIvrItem",
    "SipIvrMenu",
    "SipLatency",
    "SipLatencySpeculative",
    "SipRecordings",
    "SipServer",
    "is_env_enabled",
    "load_sip_config",
]
