"""Tests for glados.sip.audit — SIP-call audit emitters."""
from __future__ import annotations

import pathlib
import time
from unittest.mock import patch

import pytest

import sys
from glados.observability.audit import AuditEvent, AuditLogger, Origin
from glados.sip.audit import emit_call_ended, emit_call_started, emit_pin_failure


# Workaround: glados/observability/__init__.py re-exports the `audit` function,
# which shadows the submodule of the same name in the package namespace. To
# patch _LOGGER inside the actual module, fetch the module object via sys.modules.
audit_mod = sys.modules["glados.observability.audit"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_logger(tmp_path: pathlib.Path):
    """Replace the singleton with a real on-disk AuditLogger pointed at tmp_path."""
    log_file = tmp_path / "audit.jsonl"
    inst = AuditLogger(path=log_file, enabled=True)
    # AuditLogger auto-starts its background thread in __init__.
    with patch.object(audit_mod, "_LOGGER", inst):
        yield inst, log_file
    inst.shutdown(timeout=2.0)


def _read_lines(log_file: pathlib.Path) -> list[dict]:
    import json
    if not log_file.exists():
        return []
    return [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# emit_call_started
# ---------------------------------------------------------------------------

def test_call_started_emits_audit_row_with_origin_sip(captured_logger) -> None:
    inst, log_file = captured_logger
    emit_call_started(
        call_id="call_001",
        direction="inbound",
        remote_caller_id="Operator Mobile",
        remote_aor="sip:operator@192.168.1.1",
    )
    inst.shutdown(timeout=2.0)
    rows = _read_lines(log_file)
    assert len(rows) == 1
    assert rows[0]["origin"] == Origin.SIP
    assert rows[0]["principal"] == "Operator Mobile"
    extra = rows[0]["extra"]
    assert extra["event"] == "call_started"
    assert extra["call_id"] == "call_001"
    assert extra["direction"] == "inbound"


def test_call_started_falls_back_to_aor_when_no_caller_id(captured_logger) -> None:
    inst, log_file = captured_logger
    emit_call_started(
        call_id="call_002",
        direction="inbound",
        remote_aor="sip:unknown@host",
    )
    inst.shutdown(timeout=2.0)
    rows = _read_lines(log_file)
    assert rows[0]["principal"] == "sip:unknown@host"


# ---------------------------------------------------------------------------
# emit_pin_failure
# ---------------------------------------------------------------------------

def test_pin_failure_emits_refusal_kind(captured_logger) -> None:
    inst, log_file = captured_logger
    emit_pin_failure(
        call_id="call_003",
        attempts_made=1,
        attempts_remaining=2,
        via="dtmf",
    )
    inst.shutdown(timeout=2.0)
    rows = _read_lines(log_file)
    assert len(rows) == 1
    assert rows[0]["origin"] == Origin.SIP
    assert rows[0]["kind"] == "refusal"
    assert rows[0]["extra"]["event"] == "pin_failure"
    assert rows[0]["extra"]["attempts_made"] == 1
    assert rows[0]["extra"]["via"] == "dtmf"


# ---------------------------------------------------------------------------
# emit_call_ended
# ---------------------------------------------------------------------------

def test_call_ended_includes_duration_and_outcome(captured_logger) -> None:
    inst, log_file = captured_logger
    emit_call_ended(
        call_id="call_004",
        direction="inbound",
        duration_s=167.32,
        pin_outcome="accepted",
        recording_path="media/sip-recordings/call_004.mp3",
        final_state="bye",
        remote_caller_id="Operator Mobile",
    )
    inst.shutdown(timeout=2.0)
    rows = _read_lines(log_file)
    assert len(rows) == 1
    assert rows[0]["origin"] == Origin.SIP
    extra = rows[0]["extra"]
    assert extra["event"] == "call_ended"
    assert extra["duration_s"] == 167.32
    assert extra["pin_outcome"] == "accepted"
    assert extra["recording_path"] == "media/sip-recordings/call_004.mp3"
    assert extra["final_state"] == "bye"


def test_call_ended_rounds_duration_to_two_decimals(captured_logger) -> None:
    inst, log_file = captured_logger
    emit_call_ended(
        call_id="call_005",
        direction="inbound",
        duration_s=167.323456,
        pin_outcome="accepted",
    )
    inst.shutdown(timeout=2.0)
    rows = _read_lines(log_file)
    assert rows[0]["extra"]["duration_s"] == 167.32


# ---------------------------------------------------------------------------
# Origin.SIP is registered in the canonical set
# ---------------------------------------------------------------------------

def test_origin_sip_in_canonical_set() -> None:
    assert Origin.SIP == "sip"
    assert Origin.SIP in Origin.ALL


# ---------------------------------------------------------------------------
# No-op when audit logger isn't initialized
# ---------------------------------------------------------------------------

def test_emit_with_no_audit_logger_doesnt_crash(monkeypatch) -> None:
    """If the global audit logger hasn't been initialised, emits become no-ops."""
    monkeypatch.setattr(audit_mod, "_LOGGER", None)
    emit_call_started(call_id="x", direction="inbound")
    emit_pin_failure(call_id="x", attempts_made=1, attempts_remaining=2, via="dtmf")
    emit_call_ended(call_id="x", direction="inbound", duration_s=0, pin_outcome="aborted")
    # No assertion needed — just verifying no exception
