"""Unit tests for glados.observability.audit."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from glados.observability.audit import (
    AuditEvent,
    AuditLogger,
    Origin,
    audit,
    get_audit_logger,
    init_audit_logger,
)


def _read_all_lines(path: Path, timeout_s: float = 2.0) -> list[dict]:
    """Read audit.jsonl, parsing each non-empty line as JSON.

    Waits up to timeout_s for the background writer to flush. Returns
    parsed records in file order."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            break
        time.sleep(0.05)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


class TestAuditEvent:
    def test_required_fields_always_serialized(self) -> None:
        event = AuditEvent(ts=1234.5, origin=Origin.API_CHAT, kind="utterance")
        decoded = json.loads(event.to_json_line())
        assert decoded == {"ts": 1234.5, "origin": "api_chat", "kind": "utterance"}

    def test_optional_fields_elided_when_empty(self) -> None:
        event = AuditEvent(
            ts=1.0,
            origin=Origin.WEBUI_CHAT,
            kind="utterance",
            principal=None,
            utterance="",  # Empty string is falsy -> elided
            entity_ids=[],
            candidates=[],
            extra={},
        )
        decoded = json.loads(event.to_json_line())
        # Empty string currently kept (truthiness), empty list/dict elided.
        # Assert the keys that matter: optional empties don't appear.
        assert "entity_ids" not in decoded
        assert "candidates" not in decoded
        assert "extra" not in decoded
        assert "principal" not in decoded

    def test_full_event_round_trips(self) -> None:
        event = AuditEvent(
            ts=100.0,
            origin=Origin.API_CHAT,
            kind="tool_call",
            principal="session-abc",
            utterance="turn off kitchen lights",
            tier=1,
            tool="mcp.home_assistant.CallService",
            params={"domain": "light", "service": "turn_off"},
            entity_ids=["light.kitchen_ceiling"],
            result="ok",
            latency_ms=812,
            allowlist_decision="allow",
        )
        decoded = json.loads(event.to_json_line())
        assert decoded["origin"] == "api_chat"
        assert decoded["kind"] == "tool_call"
        assert decoded["tool"] == "mcp.home_assistant.CallService"
        assert decoded["entity_ids"] == ["light.kitchen_ceiling"]
        assert decoded["latency_ms"] == 812


class TestAuditLogger:
    def test_enabled_logger_writes_to_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        logger_instance = AuditLogger(path=path, enabled=True)
        try:
            logger_instance.log(AuditEvent(
                ts=1.0, origin=Origin.API_CHAT, kind="utterance", utterance="hello"
            ))
            logger_instance.log(AuditEvent(
                ts=2.0, origin=Origin.WEBUI_CHAT, kind="tool_call", tool="mcp.foo"
            ))
        finally:
            logger_instance.shutdown()

        records = _read_all_lines(path)
        assert len(records) == 2
        assert records[0]["origin"] == "api_chat"
        assert records[1]["tool"] == "mcp.foo"

    def test_disabled_logger_writes_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        logger_instance = AuditLogger(path=path, enabled=False)
        try:
            logger_instance.log(AuditEvent(ts=1.0, origin=Origin.API_CHAT, kind="utterance"))
        finally:
            logger_instance.shutdown()
        assert not path.exists()

    def test_unknown_origin_normalized_to_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        logger_instance = AuditLogger(path=path, enabled=True)
        try:
            logger_instance.log(AuditEvent(
                ts=1.0, origin="from_mars", kind="utterance"
            ))
        finally:
            logger_instance.shutdown()
        records = _read_all_lines(path)
        assert len(records) == 1
        assert records[0]["origin"] == "unknown"

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deeper" / "audit.jsonl"
        logger_instance = AuditLogger(path=path, enabled=True)
        try:
            logger_instance.log(AuditEvent(ts=1.0, origin=Origin.API_CHAT, kind="utterance"))
        finally:
            logger_instance.shutdown()
        records = _read_all_lines(path)
        assert len(records) == 1

    def test_concurrent_writers(self, tmp_path: Path) -> None:
        """Many threads writing concurrently should not corrupt lines."""
        import threading as _t

        path = tmp_path / "audit.jsonl"
        logger_instance = AuditLogger(path=path, enabled=True)
        n_threads = 8
        n_per_thread = 50

        def _writer(tid: int) -> None:
            for i in range(n_per_thread):
                logger_instance.log(AuditEvent(
                    ts=float(tid * 1000 + i),
                    origin=Origin.API_CHAT,
                    kind="utterance",
                    utterance=f"thread-{tid}-msg-{i}",
                ))

        threads = [_t.Thread(target=_writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        logger_instance.shutdown(timeout=5.0)

        records = _read_all_lines(path, timeout_s=3.0)
        assert len(records) == n_threads * n_per_thread
        # Every line parsed as JSON without error -> no interleaved writes.


class TestSingleton:
    def test_init_and_get(self, tmp_path: Path) -> None:
        # Reset any prior singleton from another test.
        path = tmp_path / "audit.jsonl"
        init_audit_logger(path=path, enabled=True)
        try:
            assert get_audit_logger() is not None
            audit(AuditEvent(ts=1.0, origin=Origin.API_CHAT, kind="utterance"))
        finally:
            singleton = get_audit_logger()
            if singleton is not None:
                singleton.shutdown()

        records = _read_all_lines(path)
        assert len(records) == 1

    def test_audit_shortcut_silently_noops_when_not_initialized(self) -> None:
        # Replace singleton with None by re-init disabled.
        # (There's no public "clear" — we just init a disabled one.)
        init_audit_logger(path="/tmp/should-not-exist.jsonl", enabled=False)
        # Calling audit() must not raise even though writes go nowhere.
        audit(AuditEvent(ts=1.0, origin=Origin.API_CHAT, kind="utterance"))


class TestOriginConstants:
    def test_all_constants_are_in_all_set(self) -> None:
        # Reflection-ish — anything that looks like a constant (UPPER_CASE str attr)
        # and is a string value should be in Origin.ALL.
        for name in dir(Origin):
            if name.startswith("_") or name == "ALL":
                continue
            value = getattr(Origin, name)
            if isinstance(value, str):
                assert value in Origin.ALL, f"{name}={value!r} missing from Origin.ALL"
