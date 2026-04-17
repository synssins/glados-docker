"""Audit logging for user-initiated actions.

Phase 0 of Stage 3. Writes a structured JSON-lines record for every
utterance that enters the system and every tool call that executes on
behalf of a user, so operators can see what origin (WebUI, API,
voice, MQTT, autonomy) caused what action.

The audit log is a distinct concern from ObservabilityBus:
  - ObservabilityBus: ephemeral in-memory event stream, generic (kind,
    source, message, meta). Powers the live WebUI activity view.
  - AuditLogger: durable JSON-lines on disk, typed schema, long
    retention. Powers forensic review, security audits, and post-hoc
    analysis of disambiguation choices.

Both are written; both are useful. The bus is for "what is happening
now"; audit is for "what happened, who caused it, and why."

Thread-safe. Writer uses a background thread and a bounded queue so
hot paths (tool_executor, api_wrapper) never block on disk I/O.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# Origin — canonical values for the `origin` field
# ---------------------------------------------------------------------------

class Origin:
    """Canonical origin values. Strings, not an enum, to keep the audit
    record JSON-friendly and forward-compatible with origins added later."""

    WEBUI_CHAT = "webui_chat"       # WebUI chat pane -> /api/chat on port 8052
    API_CHAT = "api_chat"           # External caller -> /v1/chat/completions on 8015
    VOICE_MIC = "voice_mic"         # STT pipeline (Stage 4, not yet wired)
    TEXT_STDIN = "text_stdin"       # TextListener (dev/container stdin)
    AUTONOMY = "autonomy"           # Self-triggered autonomy loop
    DISCORD = "discord"             # Discord bridge module
    MQTT_CMD = "mqtt_cmd"           # MQTT peer bus (Phase 2, not yet wired)
    UNKNOWN = "unknown"             # Caller didn't set an origin — audit-visible tell

    ALL = frozenset({
        WEBUI_CHAT, API_CHAT, VOICE_MIC, TEXT_STDIN,
        AUTONOMY, DISCORD, MQTT_CMD, UNKNOWN,
    })


# ---------------------------------------------------------------------------
# AuditEvent — one record per logged action
# ---------------------------------------------------------------------------

@dataclass
class AuditEvent:
    """One row of the audit log.

    Schema is intentionally wide so tiers 1, 2, 3 can all use the same
    record type. Fields that do not apply to a given event are left as
    None / empty and are elided from the serialized JSON when falsy."""

    ts: float                                   # Unix epoch seconds, float
    origin: str                                 # One of Origin.* values
    kind: str                                   # "utterance" | "tool_call" | "intent" | "refusal"
    principal: str | None = None                # Session id, username, broker user, etc.
    utterance: str | None = None                # Raw user text (trimmed upstream if PII-sensitive)
    tier: int | None = None                     # 1 | 2 | 3 — Stage 3 tier that handled this
    tool: str | None = None                     # Tool / service name invoked
    params: dict[str, Any] | None = None        # Tool args / service data
    entity_ids: list[str] | None = None         # Resolved HA entities (if applicable)
    candidates: list[dict[str, Any]] | None = None  # For disambiguation audit
    result: str | None = None                   # "ok" | "error" | "refused" | "timeout" | short desc
    latency_ms: int | None = None               # End-to-end latency for this event
    allowlist_decision: str | None = None       # "allow" | "deny" | None if not evaluated
    extra: dict[str, Any] = field(default_factory=dict)  # Escape hatch for uncommon fields

    def to_json_line(self) -> str:
        """Serialize to a single line of JSON, eliding empty optional fields."""
        d = asdict(self)
        # Elide falsy optional fields to keep lines compact and readable.
        # Keep ts/origin/kind always; they're required context.
        for k in list(d.keys()):
            if k in {"ts", "origin", "kind"}:
                continue
            if d[k] is None or d[k] == {} or d[k] == []:
                del d[k]
        return json.dumps(d, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# AuditLogger — bounded-queue background writer
# ---------------------------------------------------------------------------

class AuditLogger:
    """Thread-safe JSON-lines audit writer.

    Caller invokes `log(event)` which enqueues the event; a background
    thread drains the queue to disk. If the queue fills (e.g. disk is
    slow or wedged), new events are dropped and a warning is emitted to
    the logger — we never block a hot request path on disk I/O.
    """

    # If the queue exceeds this size, log() drops new events rather than
    # blocking. 10k is plenty for a home assistant's traffic.
    _QUEUE_MAXSIZE = 10_000

    def __init__(
        self,
        path: str | os.PathLike[str],
        enabled: bool = True,
    ) -> None:
        self._path = Path(path)
        self._enabled = enabled
        self._q: queue.Queue[AuditEvent] = queue.Queue(maxsize=self._QUEUE_MAXSIZE)
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._dropped = 0  # Cumulative dropped-event counter for diagnostics

        if enabled:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._thread = threading.Thread(
                target=self._run, name="AuditLogger", daemon=True
            )
            self._thread.start()
            logger.info("AuditLogger writing to {}", self._path)
        else:
            logger.info("AuditLogger disabled by config")

    def log(self, event: AuditEvent) -> None:
        """Enqueue an event. Non-blocking. Drops on queue full."""
        if not self._enabled:
            return
        if event.origin not in Origin.ALL:
            # Don't crash the caller; record the violation and normalize.
            logger.warning("AuditLogger: unknown origin '{}' — recording as 'unknown'", event.origin)
            event.origin = Origin.UNKNOWN
        try:
            self._q.put_nowait(event)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                # Log only every 100th drop to avoid log spam under sustained pressure.
                logger.warning(
                    "AuditLogger: queue full, dropped {} events so far", self._dropped
                )

    def shutdown(self, timeout: float = 2.0) -> None:
        """Flush pending events and stop the writer thread."""
        self._shutdown.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        """Writer thread: drain the queue to disk."""
        # Open in append mode; one line per event; fsync not needed per-line
        # (OS page cache is fine for a home-audit log).
        try:
            fp = self._path.open("a", encoding="utf-8", buffering=1)  # line-buffered
        except OSError as exc:
            logger.error("AuditLogger: cannot open {}: {}", self._path, exc)
            return

        try:
            while not self._shutdown.is_set():
                try:
                    event = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    fp.write(event.to_json_line() + "\n")
                except (OSError, ValueError) as exc:
                    logger.error("AuditLogger: write failed: {}", exc)
            # Drain remaining events on shutdown, best-effort.
            while True:
                try:
                    event = self._q.get_nowait()
                except queue.Empty:
                    break
                try:
                    fp.write(event.to_json_line() + "\n")
                except (OSError, ValueError):
                    break
        finally:
            try:
                fp.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Module-level singleton access
# ---------------------------------------------------------------------------

_LOGGER: AuditLogger | None = None
_LOGGER_LOCK = threading.Lock()


def init_audit_logger(path: str | os.PathLike[str], enabled: bool = True) -> AuditLogger:
    """Create or replace the process-wide audit logger.

    Called once during engine startup from `config_store` or `server.py`.
    Subsequent calls replace the previous logger (old one is shut down).
    """
    global _LOGGER
    with _LOGGER_LOCK:
        if _LOGGER is not None:
            _LOGGER.shutdown()
        _LOGGER = AuditLogger(path=path, enabled=enabled)
        return _LOGGER


def get_audit_logger() -> AuditLogger | None:
    """Return the process-wide audit logger, or None if not yet initialized."""
    return _LOGGER


def audit(event: AuditEvent) -> None:
    """Convenience shortcut: log an event through the singleton, if any."""
    logger_instance = _LOGGER
    if logger_instance is not None:
        logger_instance.log(event)


def now() -> float:
    """Uniform timestamp source so tests can monkeypatch."""
    return time.time()
