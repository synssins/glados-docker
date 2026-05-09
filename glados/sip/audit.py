"""SIP call audit logging.

Thin emitter that constructs ``AuditEvent`` instances for SIP-call
lifecycle moments and writes them through the existing
``glados.observability.audit`` infrastructure. Same retention,
threading, and JSON-lines format as the other origins.

Events emitted per call:

- ``call_started`` — INVITE accepted, recorded for forensic review
- ``call_ended`` — final state (BYE), with duration + pin_outcome +
  recording path

Optional events (call_session can emit per its needs):

- ``pin_failure`` — one row per failed PIN attempt, helpful for
  spotting brute-force attempts
"""
from __future__ import annotations

from typing import Optional

from glados.observability.audit import AuditEvent, Origin, audit, now


def emit_call_started(
    *,
    call_id: str,
    direction: str,
    remote_caller_id: str | None = None,
    remote_aor: str | None = None,
) -> None:
    """Log a call-started audit row.

    direction: 'inbound' | 'outbound'
    """
    audit(AuditEvent(
        ts=now(),
        origin=Origin.SIP,
        kind="utterance",  # using existing kind enum; "call_started" goes in extra
        principal=remote_caller_id or remote_aor or "unknown",
        utterance=None,
        extra={
            "event": "call_started",
            "call_id": call_id,
            "direction": direction,
            "remote_aor": remote_aor,
            "remote_caller_id": remote_caller_id,
        },
    ))


def emit_pin_failure(
    *,
    call_id: str,
    attempts_made: int,
    attempts_remaining: int,
    via: str,  # 'dtmf' | 'stt'
) -> None:
    """Log a PIN-failure audit row. Helpful for spotting brute-force attempts."""
    audit(AuditEvent(
        ts=now(),
        origin=Origin.SIP,
        kind="refusal",
        extra={
            "event": "pin_failure",
            "call_id": call_id,
            "attempts_made": attempts_made,
            "attempts_remaining": attempts_remaining,
            "via": via,
        },
    ))


def emit_call_ended(
    *,
    call_id: str,
    direction: str,
    duration_s: float,
    pin_outcome: str,                     # 'accepted' | 'rejected' | 'aborted'
    recording_path: Optional[str] = None,
    final_state: Optional[str] = None,    # CallState string for forensics
    remote_caller_id: str | None = None,
) -> None:
    """Log a call-ended audit row with the lifecycle summary."""
    audit(AuditEvent(
        ts=now(),
        origin=Origin.SIP,
        kind="utterance",
        principal=remote_caller_id or "unknown",
        utterance=None,
        extra={
            "event": "call_ended",
            "call_id": call_id,
            "direction": direction,
            "duration_s": round(duration_s, 2),
            "pin_outcome": pin_outcome,
            "recording_path": recording_path,
            "final_state": final_state,
        },
    ))
