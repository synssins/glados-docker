"""Log groups — operator-tunable per-subsystem logging.

Loguru ships with a single global level filter, which is too coarse for
production debugging: either you accept the default-quiet behaviour and
miss diagnostic detail when something breaks, or you crank the global
level to DEBUG and drown in chatter from unrelated subsystems.

This module layers a per-group filter on top of loguru. Every diagnostic
log call binds a stable group ID (e.g. ``chat.round1_stream``); the
group's enabled flag and individual level threshold are read from the
registry on every record. The registry is hot-reloadable from
``configs/logging.yaml`` so flipping a group on/off in the WebUI takes
effect immediately.

ERROR and CRITICAL records bypass the per-group filter entirely — you
cannot accidentally silence error logging via this UI. Audit-group
records are also locked-on for the same reason.

Operator-facing surface (the WebUI Logging page) consumes:
* ``LogGroupRegistry.list_groups()`` — every group with its current state.
* ``LogGroupRegistry.set_group_state(group_id, enabled, level)`` — runtime
  toggle. Persists to YAML automatically.
* ``LogGroupRegistry.recent_activity(group_id)`` — rolling 5-minute hit
  count for the group, used to spot noisy / silent groups visually.

Code-facing surface:
* ``group_logger(LogGroupId.CHAT.ROUND1_STREAM)`` — returns a loguru
  logger bound to the group. Use exactly like loguru: ``.info(...)``,
  ``.debug(...)``, etc. Every call records the binding so the registry
  can filter / count it.

Defaults (see ``BUILTIN_GROUPS``) are baked in so the container always
boots with sane visibility even if ``configs/logging.yaml`` is missing
or corrupt. The registry tolerates parse failures by logging a WARNING
and falling back to defaults; no boot-time crash on bad YAML.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

import yaml
from loguru import logger
from pydantic import BaseModel, Field, field_validator


class LogLevel(str, Enum):
    """Loguru levels exposed through the per-group toggle.

    ERROR and CRITICAL are deliberately omitted — they are always-on by
    policy. The WebUI presents these four as the per-group dropdown.
    """

    DEBUG = "DEBUG"
    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"


_LEVEL_NUMBER: dict[str, int] = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


def _level_no(level_name: str) -> int:
    return _LEVEL_NUMBER.get(level_name.upper(), 20)


# Hardcoded — these groups exist independent of any YAML state. ``WARNING``
# floor on always-on groups protects critical-path visibility from
# accidental disable via UI; the registry refuses to honour an attempt to
# disable a locked group.
LOCKED_ON_GROUP_IDS: frozenset[str] = frozenset({"auth.audit"})


# ---------------------------------------------------------------------------
# Group ID constants
# ---------------------------------------------------------------------------
#
# Use these instead of raw strings so call sites are typo-proof and grep-able:
#
#     from glados.observability.log_groups import LogGroupId, group_logger
#     log = group_logger(LogGroupId.CHAT.ROUND1_STREAM)
#     log.info("upstream connecting: {}:{}", host, port)
#
# Adding a new group is a two-step change: add the ID here, then add the
# matching ``LogGroup`` definition in BUILTIN_GROUPS below. The registry
# refuses to load a YAML that references unknown IDs.


class _IdNamespace:
    """Helper so subclasses can be iterated to surface every public ID."""

    @classmethod
    def all(cls) -> list[str]:
        return [v for k, v in vars(cls).items() if not k.startswith("_") and isinstance(v, str)]


class LogGroupId:
    """Stable string IDs for every log group. Reference these from code."""

    class CHAT(_IdNamespace):
        ROUND1_STREAM = "chat.round1_stream"
        ROUND1_RAW_BYTES = "chat.round1_raw_bytes"
        ROUND2_STREAM = "chat.round2_stream"
        ROUND2_RAW_BYTES = "chat.round2_raw_bytes"
        TOOL_CALL = "chat.tool_call"
        TOOL_RESULT = "chat.tool_result"
        FILTER_PIPELINE = "chat.filter_pipeline"
        CONNECT_PATH = "chat.connect_path"
        SANITIZE_HISTORY = "chat.sanitize_history"
        ROUTING_DECISION = "chat.routing_decision"

    class PLUGIN(_IdNamespace):
        INTENT_MATCH = "plugin.intent_match"
        TRIAGE_LLM = "plugin.triage_llm"
        DISCOVERY = "plugin.discovery"
        BUNDLE_VALIDATION = "plugin.bundle_validation"
        RUNNER = "plugin.runner"

    class AUTONOMY(_IdNamespace):
        SLOTS = "autonomy.slots"
        WEATHER = "autonomy.weather"
        CAMERA_WATCHER = "autonomy.camera_watcher"
        HA_SENSOR_WATCHER = "autonomy.ha_sensor_watcher"
        HACKER_NEWS = "autonomy.hacker_news"
        EMOTION = "autonomy.emotion"
        MESSAGE_COMPACTION = "autonomy.message_compaction"
        OBSERVER = "autonomy.observer"
        LLM_CLIENT = "autonomy.llm_client"
        LLM_PROCESSOR = "autonomy.llm_processor"

    class HA(_IdNamespace):
        WS_CLIENT = "ha.ws_client"
        SEMANTIC_INDEX = "ha.semantic_index"
        DISAMBIGUATOR = "ha.disambiguator"
        COMMAND_RESOLVER = "ha.command_resolver"
        REGISTRY_APPLY = "ha.registry_apply"

    class MCP(_IdNamespace):
        MANAGER = "mcp.manager"
        SPAWN = "mcp.spawn"
        BUILTIN_TOOLS = "mcp.builtin_tools"

    class TTS(_IdNamespace):
        SYNTHESIZER = "tts.synthesizer"
        PIPER = "tts.piper"
        PRONUNCIATION = "tts.pronunciation"
        AUDIO_FILE_SERVER = "tts.audio_file_server"

    class MEMORY(_IdNamespace):
        CONTEXT_INJECT = "memory.context_inject"
        PASSIVE_EXTRACT = "memory.passive_extract"
        CHROMADB = "memory.chromadb"
        STORE = "memory.store"

    class WEBUI(_IdNamespace):
        API_REQUEST = "webui.api_request"
        CONFIG_SAVE = "webui.config_save"
        CLIENT_CONSOLE = "webui.client_console"
        TTS_STREAM = "webui.tts_stream"
        STATIC_FILES = "webui.static_files"

    class AUTH(_IdNamespace):
        SESSION = "auth.session"
        AUDIT = "auth.audit"

    class LIFECYCLE(_IdNamespace):
        STARTUP = "lifecycle.startup"
        SHUTDOWN = "lifecycle.shutdown"
        HEALTH = "lifecycle.health"

    class CONVERSATION(_IdNamespace):
        STORE = "conversation.store"

    class CONFIG(_IdNamespace):
        STORE = "config.store"
        CONTEXT_GATES = "config.context_gates"

    class FILTER(_IdNamespace):
        THINK_TAG = "filter.think_tag"
        BOILERPLATE = "filter.boilerplate"

    class NET(_IdNamespace):
        OUTBOUND_HTTP = "net.outbound_http"

    @classmethod
    def all_ids(cls) -> list[str]:
        out: list[str] = []
        for ns_name, ns in vars(cls).items():
            if ns_name.startswith("_") or ns_name == "all_ids":
                continue
            if isinstance(ns, type):
                out.extend(ns.all())
        return sorted(out)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class LogGroup(BaseModel):
    """Single log-group config row."""

    id: str = Field(
        ..., description="Stable machine ID (snake_case, dot-separated, e.g. chat.round1_stream)."
    )
    name: str = Field(..., description="Human-readable display name shown in the WebUI.")
    description: str = Field(default="", description="One-line description shown as tooltip.")
    category: str = Field(
        default="", description="Top-level category (Chat, Plugin, Autonomy, ...) for UI grouping."
    )
    enabled: bool = Field(default=True)
    level: LogLevel = Field(default=LogLevel.INFO)

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not v:
            raise ValueError("group id must not be empty")
        for c in v:
            if not (c.islower() or c.isdigit() or c in "._"):
                raise ValueError(
                    f"group id must be lowercase + digits + dots/underscores; got {v!r}"
                )
        if "." not in v:
            raise ValueError(f"group id must contain a category prefix (e.g. chat.foo); got {v!r}")
        return v


class LogGroupsConfig(BaseModel):
    """Top-level shape of ``configs/logging.yaml``."""

    default_level: LogLevel = Field(
        default=LogLevel.SUCCESS,
        description=(
            "Floor for any log call that has no group binding (legacy code). Records below this "
            "level are dropped before any per-group filter runs."
        ),
    )
    groups: list[LogGroup]


# ---------------------------------------------------------------------------
# Built-in defaults
# ---------------------------------------------------------------------------


def _g(
    gid: str,
    name: str,
    description: str,
    category: str,
    enabled: bool = True,
    level: LogLevel = LogLevel.INFO,
) -> LogGroup:
    return LogGroup(
        id=gid,
        name=name,
        description=description,
        category=category,
        enabled=enabled,
        level=level,
    )


# Defaults aim for "useful in production without spamming the logs."
# Anything chatty (raw bytes, per-chunk parse, semantic-index queries) is
# disabled by default and toggled on by the operator when investigating.
BUILTIN_GROUPS: list[LogGroup] = [
    # --- Chat path ---------------------------------------------------------
    _g(
        LogGroupId.CHAT.ROUND1_STREAM,
        "Chat — Round 1 LLM Stream",
        "Per-chunk events from the user's chat request to the LLM. Summary-level "
        "diagnostics (chunks count, finish_reason, content/tool deltas).",
        "Chat",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.CHAT.ROUND1_RAW_BYTES,
        "Chat — Round 1 Raw Chunk Bytes",
        "Every SSE line from LM Studio verbatim, plus full first-chunk JSON. "
        "Very verbose — only enable when debugging chat content shape.",
        "Chat",
        enabled=False,
        level=LogLevel.DEBUG,
    ),
    _g(
        LogGroupId.CHAT.ROUND2_STREAM,
        "Chat — Round 2 LLM Stream (post-tool-result)",
        "Per-chunk events from the post-tool-result LLM call. Same shape as round 1.",
        "Chat",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.CHAT.ROUND2_RAW_BYTES,
        "Chat — Round 2 Raw Chunk Bytes",
        "Every SSE line from the round-2 stream verbatim. Pair with round-1 raw bytes.",
        "Chat",
        enabled=False,
        level=LogLevel.DEBUG,
    ),
    _g(
        LogGroupId.CHAT.TOOL_CALL,
        "Chat — Tool Call Dispatch",
        "Each tool the model invokes during the agentic loop, with args + latency.",
        "Chat",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.CHAT.TOOL_RESULT,
        "Chat — Tool Result Body",
        "First 500 chars of each tool's result body. Disabled by default — tool "
        "results can be large.",
        "Chat",
        enabled=False,
        level=LogLevel.DEBUG,
    ),
    _g(
        LogGroupId.CHAT.FILTER_PIPELINE,
        "Chat — <think> Tag Filter Pipeline",
        "State transitions in the streaming filter that strips reasoning content.",
        "Chat",
        enabled=False,
        level=LogLevel.DEBUG,
    ),
    _g(
        LogGroupId.CHAT.CONNECT_PATH,
        "Chat — LLM Upstream Connect / Status",
        "TCP/HTTP connect attempts to LM Studio, response status + reason, error "
        "bodies. Should always be on.",
        "Chat",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.CHAT.SANITIZE_HISTORY,
        "Chat — Message History Sanitization",
        "Detail of which roles / fields the sanitizer dropped before sending to "
        "the LLM. Enable when chat history shape looks wrong.",
        "Chat",
        enabled=False,
        level=LogLevel.DEBUG,
    ),
    _g(
        LogGroupId.CHAT.ROUTING_DECISION,
        "Chat — Routing Decision (HA / plugin / chitchat)",
        "Which path the chat handler chose and why. Includes is_home_command "
        "result and plugin match list.",
        "Chat",
        enabled=True,
        level=LogLevel.INFO,
    ),
    # --- Plugin layer ------------------------------------------------------
    _g(
        LogGroupId.PLUGIN.INTENT_MATCH,
        "Plugin — Keyword Intent Matcher",
        "Which keyword triggered which plugin via which stem.",
        "Plugin",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.PLUGIN.TRIAGE_LLM,
        "Plugin — LLM Triage Classifier",
        "Triage LLM invocation, latency, raw response, parsed plugin list, "
        "hallucinated-name drops.",
        "Plugin",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.PLUGIN.DISCOVERY,
        "Plugin — Bundle Discovery",
        "Each plugin.json / runtime.yaml / secrets.env file load.",
        "Plugin",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.PLUGIN.BUNDLE_VALIDATION,
        "Plugin — Bundle Schema Validation",
        "PluginJSON pass/fail with field-level detail.",
        "Plugin",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.PLUGIN.RUNNER,
        "Plugin — Runner / MCPServerConfig Translation",
        "Translation steps from plugin manifest to MCP server config.",
        "Plugin",
        enabled=False,
        level=LogLevel.DEBUG,
    ),
    # --- Autonomy ---------------------------------------------------------
    _g(
        LogGroupId.AUTONOMY.SLOTS,
        "Autonomy — Slot State Transitions",
        "Every slot transition with the reason. Used to debug 'why is slot X stuck'.",
        "Autonomy",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.AUTONOMY.WEATHER,
        "Autonomy — Weather Subagent",
        "Weather fetch attempts, decision rationale, fallback paths.",
        "Autonomy",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.AUTONOMY.CAMERA_WATCHER,
        "Autonomy — Camera Watcher",
        "Camera-events poll attempts. Tends to be noisy when the vision service is down.",
        "Autonomy",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.AUTONOMY.HA_SENSOR_WATCHER,
        "Autonomy — HA Sensor Watcher",
        "Home Assistant sensor poll cycle.",
        "Autonomy",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.AUTONOMY.HACKER_NEWS,
        "Autonomy — Hacker News Subagent",
        "HN fetch + summarization decisions.",
        "Autonomy",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.AUTONOMY.EMOTION,
        "Autonomy — Emotion State / PAD Math",
        "Emotion event push, repetition counter, semantic clustering, command-flood "
        "detector triggers.",
        "Autonomy",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.AUTONOMY.MESSAGE_COMPACTION,
        "Autonomy — Message Compaction",
        "Compaction trigger threshold, before/after token counts, summary content.",
        "Autonomy",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.AUTONOMY.OBSERVER,
        "Autonomy — Observer Agent",
        "ObserverAgent decision rationale.",
        "Autonomy",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.AUTONOMY.LLM_CLIENT,
        "Autonomy — LLM Client Calls",
        "Every outbound LLM call from autonomy slots. URL, model, slot, payload size, "
        "response timing, retries.",
        "Autonomy",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.AUTONOMY.LLM_PROCESSOR,
        "Autonomy — LLM Processor",
        "Chunk processing for autonomy LLM responses, including error-shape chunks.",
        "Autonomy",
        enabled=True,
        level=LogLevel.INFO,
    ),
    # --- Home Assistant ----------------------------------------------------
    _g(
        LogGroupId.HA.WS_CLIENT,
        "HA — WebSocket Client",
        "Connect / auth / subscribe lifecycle, every state event, reconnect, close.",
        "HA",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.HA.SEMANTIC_INDEX,
        "HA — Semantic Entity Index",
        "Build steps, dim, entity counts, persistence I/O, query top-K.",
        "HA",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.HA.DISAMBIGUATOR,
        "HA — Tier 2 Disambiguator",
        "Tier 2 invocation prompt, raw response, parsed candidates, final selection.",
        "HA",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.HA.COMMAND_RESOLVER,
        "HA — Command Resolver / Learned Context",
        "Learned-context lookup, user-pref overrides, fuzzy match scores.",
        "HA",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.HA.REGISTRY_APPLY,
        "HA — Registry Application",
        "Entity / device / area registry application — counts, deltas vs prior.",
        "HA",
        enabled=False,
        level=LogLevel.INFO,
    ),
    # --- MCP ---------------------------------------------------------------
    _g(
        LogGroupId.MCP.MANAGER,
        "MCP — Tool Manager / Dispatch",
        "Server selection, request payload, response, latency, error.",
        "MCP",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.MCP.SPAWN,
        "MCP — stdio Server Spawn Lifecycle",
        "uvx/npx process start, stdout/stderr stream, exit code.",
        "MCP",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.MCP.BUILTIN_TOOLS,
        "MCP — Built-in Tools (search_entities, etc.)",
        "Built-in tool dispatch path that bypasses MCP transport.",
        "MCP",
        enabled=False,
        level=LogLevel.INFO,
    ),
    # --- TTS pipeline ------------------------------------------------------
    _g(
        LogGroupId.TTS.SYNTHESIZER,
        "TTS — Sentence Buffer / Chunking",
        "Sentence boundary decisions, chunk indexing.",
        "TTS",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.TTS.PIPER,
        "TTS — Piper / Speaches Calls",
        "TTS backend invocations with timing.",
        "TTS",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.TTS.PRONUNCIATION,
        "TTS — Pronunciation Override Pipeline",
        "Container-side pronunciation rules and matches.",
        "TTS",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.TTS.AUDIO_FILE_SERVER,
        "TTS — Audio File Server",
        "Port-5051 audio file serving, cache hits/misses.",
        "TTS",
        enabled=False,
        level=LogLevel.INFO,
    ),
    # --- Memory ------------------------------------------------------------
    _g(
        LogGroupId.MEMORY.CONTEXT_INJECT,
        "Memory — Chat Context Injection",
        "Injection of memory context into the chat prompt. Char count + content "
        "preview at DEBUG.",
        "Memory",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.MEMORY.PASSIVE_EXTRACT,
        "Memory — Passive Fact Extraction",
        "Background fact-extraction pipeline (proactive_memory.passive).",
        "Memory",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.MEMORY.CHROMADB,
        "Memory — ChromaDB Read/Write",
        "Every ChromaDB query / persist with key + size + latency.",
        "Memory",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.MEMORY.STORE,
        "Memory — Memory Store",
        "MemoryStore reads / writes / evictions.",
        "Memory",
        enabled=False,
        level=LogLevel.INFO,
    ),
    # --- WebUI -------------------------------------------------------------
    _g(
        LogGroupId.WEBUI.API_REQUEST,
        "WebUI — Server-Side API Request Handlers",
        "Every API route entry/exit with method, path, status, latency.",
        "WebUI",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.WEBUI.CONFIG_SAVE,
        "WebUI — Configuration Save Events",
        "Each YAML save with diff vs prior. High-signal for 'who changed what'.",
        "WebUI",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.WEBUI.CLIENT_CONSOLE,
        "WebUI — Client-Side Console (forwarded from browser)",
        "Browser console.log / .error forwarded via /api/log so client logs land "
        "in the same docker logs as server logs.",
        "WebUI",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.WEBUI.TTS_STREAM,
        "WebUI — TTS Streaming Pipeline",
        "TTS UI's main streaming loop: attitude events, LLM metrics events, TTS "
        "chunk synthesis, emotion injection, timing.",
        "WebUI",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.WEBUI.STATIC_FILES,
        "WebUI — Static File Serving",
        "Static asset GETs.",
        "WebUI",
        enabled=False,
        level=LogLevel.INFO,
    ),
    # --- Auth --------------------------------------------------------------
    _g(
        LogGroupId.AUTH.SESSION,
        "Auth — Session Validation",
        "Per-request session check. Useful when login/logout is acting up.",
        "Auth",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.AUTH.AUDIT,
        "Auth — Audit Events (login / role change / lockout)",
        "Always-on by policy. The toggle is locked.",
        "Auth",
        enabled=True,
        level=LogLevel.SUCCESS,
    ),
    # --- Lifecycle ---------------------------------------------------------
    _g(
        LogGroupId.LIFECYCLE.STARTUP,
        "Lifecycle — Container Startup",
        "Init steps (HA WS, semantic index, MCP, plugins, autonomy, audio, TTS, "
        "ASR, ChromaDB, port binds) — entry, exit, latency.",
        "Lifecycle",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.LIFECYCLE.SHUTDOWN,
        "Lifecycle — Container Shutdown / SIGTERM",
        "Shutdown orchestration steps.",
        "Lifecycle",
        enabled=True,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.LIFECYCLE.HEALTH,
        "Lifecycle — Health Endpoint",
        "What /health actually checks each cycle.",
        "Lifecycle",
        enabled=False,
        level=LogLevel.INFO,
    ),
    # --- Misc -------------------------------------------------------------
    _g(
        LogGroupId.CONVERSATION.STORE,
        "Conversation Store — Append / Load / Prune",
        "Every conversation-store operation with content preview.",
        "Conversation",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.CONFIG.STORE,
        "Config Store — YAML Load / Save",
        "Every YAML file load / save with parse outcome.",
        "Config",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.CONFIG.CONTEXT_GATES,
        "Config — Context Gates",
        "context_gates.yaml resolution — currently warns on missing.",
        "Config",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.FILTER.THINK_TAG,
        "Filter — Streaming <think> Tag State",
        "Per-byte state machine for the <think>...</think> filter.",
        "Filter",
        enabled=False,
        level=LogLevel.DEBUG,
    ),
    _g(
        LogGroupId.FILTER.BOILERPLATE,
        "Filter — Closing Boilerplate Stripper",
        "When strip_closing_boilerplate matches, with the text it stripped.",
        "Filter",
        enabled=False,
        level=LogLevel.INFO,
    ),
    _g(
        LogGroupId.NET.OUTBOUND_HTTP,
        "Network — Outbound HTTP Calls",
        "Generic wrapper logs for any outbound HTTP call (URL, method, body size, "
        "response status, response size, latency).",
        "Network",
        enabled=False,
        level=LogLevel.INFO,
    ),
]


_BUILTIN_BY_ID: dict[str, LogGroup] = {g.id: g for g in BUILTIN_GROUPS}


# ---------------------------------------------------------------------------
# Activity counter
# ---------------------------------------------------------------------------


@dataclass
class _ActivityCounter:
    """Rolling 5-minute hit counter per group. Thread-safe."""

    window_seconds: float = 300.0
    _hits: dict[str, deque[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, group_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            buf = self._hits.setdefault(group_id, deque())
            buf.append(now)
            cutoff = now - self.window_seconds
            while buf and buf[0] < cutoff:
                buf.popleft()

    def count(self, group_id: str) -> int:
        with self._lock:
            buf = self._hits.get(group_id)
            if not buf:
                return 0
            now = time.monotonic()
            cutoff = now - self.window_seconds
            while buf and buf[0] < cutoff:
                buf.popleft()
            return len(buf)

    def snapshot(self) -> dict[str, int]:
        out: dict[str, int] = {}
        with self._lock:
            now = time.monotonic()
            cutoff = now - self.window_seconds
            for gid, buf in self._hits.items():
                while buf and buf[0] < cutoff:
                    buf.popleft()
                out[gid] = len(buf)
        return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class LogGroupRegistry:
    """Thread-safe runtime registry of log groups.

    The registry is the single source of truth at runtime. The on-disk
    YAML is the source of truth across restarts. ``set_group_state`` and
    ``replace_config`` write to disk atomically and notify the loguru
    filter via the in-memory state — no restart needed for changes.
    """

    def __init__(self, config: LogGroupsConfig, *, persistence_path: Path | None = None) -> None:
        self._config = config
        self._by_id: dict[str, LogGroup] = {g.id: g for g in config.groups}
        self._lock = threading.RLock()
        self._activity = _ActivityCounter()
        self._persistence_path = persistence_path
        self._global_override_level: str | None = None
        self._sync_global_override_from_env()

    # -- construction ------------------------------------------------------

    @classmethod
    def defaults(cls, *, persistence_path: Path | None = None) -> "LogGroupRegistry":
        return cls(
            LogGroupsConfig(default_level=LogLevel.SUCCESS, groups=list(BUILTIN_GROUPS)),
            persistence_path=persistence_path,
        )

    @classmethod
    def from_yaml(
        cls,
        path: Path,
        *,
        warn_on_missing: bool = True,
    ) -> "LogGroupRegistry":
        """Load from YAML. Falls back to defaults on any failure.

        We never crash the container on a bad logging config — diagnostic
        infrastructure should be resilient. Bad YAML produces a WARNING and
        we proceed with builtins. The bad file is preserved as
        ``<name>.broken-<timestamp>`` so the operator can see what failed.
        """
        if not path.exists():
            if warn_on_missing:
                logger.warning(
                    "log_groups: {} not found; using built-in defaults", path
                )
            return cls.defaults(persistence_path=path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            cfg = LogGroupsConfig.model_validate(raw)
        except Exception as exc:
            logger.warning(
                "log_groups: failed to parse {} ({}); falling back to defaults", path, exc
            )
            try:
                backup = path.with_suffix(path.suffix + f".broken-{int(time.time())}")
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                logger.warning("log_groups: bad config preserved at {}", backup)
            except Exception:
                pass
            return cls.defaults(persistence_path=path)
        # Merge with builtins so newly-added groups (e.g. after a deploy) get
        # picked up even if the on-disk YAML predates them.
        merged = cls._merge_with_builtins(cfg.groups)
        cfg = LogGroupsConfig(default_level=cfg.default_level, groups=merged)
        return cls(cfg, persistence_path=path)

    @staticmethod
    def _merge_with_builtins(stored: list[LogGroup]) -> list[LogGroup]:
        """Take stored config and overlay any new builtin groups it lacks.

        Existing stored entries win for ``enabled`` / ``level`` (operator
        intent), but builtins win for ``name`` / ``description`` /
        ``category`` so docs updates flow through without the operator
        having to re-export.
        """
        by_id = {g.id: g for g in stored}
        merged: list[LogGroup] = []
        seen: set[str] = set()
        for builtin in BUILTIN_GROUPS:
            seen.add(builtin.id)
            if builtin.id in by_id:
                stored_g = by_id[builtin.id]
                merged.append(
                    LogGroup(
                        id=builtin.id,
                        name=builtin.name,
                        description=builtin.description,
                        category=builtin.category,
                        enabled=stored_g.enabled,
                        level=stored_g.level,
                    )
                )
            else:
                merged.append(builtin)
        # Drop any stored groups whose IDs are no longer in builtins (renamed
        # / removed). We don't want to keep zombie entries in the YAML.
        for orphan_id in set(by_id) - seen:
            logger.warning(
                "log_groups: dropping orphan group {!r} (no longer a builtin ID)",
                orphan_id,
            )
        return merged

    # -- env override -----------------------------------------------------

    def _sync_global_override_from_env(self) -> None:
        env = os.environ.get("GLADOS_LOG_LEVEL")
        if env:
            env = env.strip().upper()
            if env in _LEVEL_NUMBER:
                self._global_override_level = env
            else:
                logger.warning(
                    "log_groups: GLADOS_LOG_LEVEL={!r} not recognised; ignoring", env
                )

    @property
    def global_override_level(self) -> str | None:
        return self._global_override_level

    # -- core query --------------------------------------------------------

    def get(self, group_id: str) -> LogGroup | None:
        with self._lock:
            return self._by_id.get(group_id)

    def list_groups(self) -> list[LogGroup]:
        with self._lock:
            return list(self._by_id.values())

    @property
    def default_level(self) -> str:
        with self._lock:
            return self._config.default_level.value

    @property
    def default_level_no(self) -> int:
        return _level_no(self.default_level)

    def is_locked(self, group_id: str) -> bool:
        return group_id in LOCKED_ON_GROUP_IDS

    # -- filter decision (called per loguru record) ------------------------

    def decide(self, group_id: str | None, record_level_no: int) -> bool:
        """Loguru filter consults this. Returns True if the record should pass."""
        # ERROR / CRITICAL bypass per-group filtering entirely.
        if record_level_no >= _LEVEL_NUMBER["ERROR"]:
            return True
        # Global override (env var) — when set, every group sees DOWN to that
        # level. Useful for a one-shot "show me everything" deployment.
        override = self._global_override_level
        if override is not None and record_level_no < _level_no(override):
            return False
        with self._lock:
            if group_id is None:
                return record_level_no >= self.default_level_no
            grp = self._by_id.get(group_id)
            if grp is None:
                # Unknown group ID — pass at default level. This is the
                # forgiving behaviour; the alternative (drop) would silently
                # hide logs after a code-only change that hasn't pushed a
                # YAML update.
                return record_level_no >= self.default_level_no
            if not grp.enabled and not self.is_locked(group_id):
                return False
            return record_level_no >= _level_no(grp.level.value)

    def record_hit(self, group_id: str | None) -> None:
        if group_id:
            self._activity.record(group_id)

    def recent_activity(self, group_id: str) -> int:
        return self._activity.count(group_id)

    def all_recent_activity(self) -> dict[str, int]:
        return self._activity.snapshot()

    # -- mutation ----------------------------------------------------------

    def set_group_state(
        self,
        group_id: str,
        *,
        enabled: bool | None = None,
        level: LogLevel | str | None = None,
    ) -> LogGroup:
        with self._lock:
            existing = self._by_id.get(group_id)
            if existing is None:
                raise KeyError(f"unknown log group {group_id!r}")
            if self.is_locked(group_id):
                if enabled is False:
                    raise PermissionError(
                        f"log group {group_id!r} is locked-on by policy and cannot be disabled"
                    )
            if isinstance(level, str):
                level_obj = LogLevel(level.upper())
            elif isinstance(level, LogLevel):
                level_obj = level
            else:
                level_obj = existing.level
            updated = LogGroup(
                id=existing.id,
                name=existing.name,
                description=existing.description,
                category=existing.category,
                enabled=enabled if enabled is not None else existing.enabled,
                level=level_obj,
            )
            self._by_id[group_id] = updated
            self._config = LogGroupsConfig(
                default_level=self._config.default_level,
                groups=list(self._by_id.values()),
            )
            self._persist_locked()
            return updated

    def set_default_level(self, level: LogLevel | str) -> None:
        with self._lock:
            level_obj = LogLevel(level.upper()) if isinstance(level, str) else level
            self._config = LogGroupsConfig(
                default_level=level_obj,
                groups=list(self._by_id.values()),
            )
            self._persist_locked()

    def replace_config(self, new_config: LogGroupsConfig) -> None:
        """Hot-swap the entire config (used by the WebUI 'Save' action).

        The new config must validate against the schema *and* every group
        ID must already be a builtin (no rogue IDs created via the UI).
        """
        unknown = [g.id for g in new_config.groups if g.id not in _BUILTIN_BY_ID]
        if unknown:
            raise ValueError(f"unknown group IDs in replacement config: {unknown}")
        with self._lock:
            self._config = LogGroupsConfig(
                default_level=new_config.default_level,
                groups=self._merge_with_builtins(new_config.groups),
            )
            self._by_id = {g.id: g for g in self._config.groups}
            self._persist_locked()

    def reload(self) -> None:
        """Re-read the persistence YAML, replacing in-memory state."""
        if self._persistence_path is None:
            return
        fresh = LogGroupRegistry.from_yaml(self._persistence_path, warn_on_missing=False)
        with self._lock:
            self._config = fresh._config
            self._by_id = fresh._by_id
            self._sync_global_override_from_env()

    def reset_to_defaults(self) -> None:
        with self._lock:
            self._config = LogGroupsConfig(
                default_level=LogLevel.SUCCESS, groups=list(BUILTIN_GROUPS)
            )
            self._by_id = {g.id: g for g in self._config.groups}
            self._persist_locked()

    # -- persistence ------------------------------------------------------

    def _persist_locked(self) -> None:
        """Atomic write. Caller must hold ``self._lock``."""
        if self._persistence_path is None:
            return
        path = self._persistence_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            payload = self._config.model_dump(mode="json")
            tmp.write_text(
                yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as exc:
            logger.error("log_groups: failed to persist {} ({})", path, exc)

    def export_yaml(self) -> str:
        with self._lock:
            payload = self._config.model_dump(mode="json")
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

    # -- iteration helpers (for the WebUI page) ---------------------------

    def by_category(self) -> dict[str, list[LogGroup]]:
        out: dict[str, list[LogGroup]] = {}
        with self._lock:
            for g in self._by_id.values():
                out.setdefault(g.category or "Other", []).append(g)
        for v in out.values():
            v.sort(key=lambda g: g.name.lower())
        return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))


# ---------------------------------------------------------------------------
# Module-level singleton + loguru wiring
# ---------------------------------------------------------------------------


_REGISTRY: LogGroupRegistry | None = None
_REGISTRY_LOCK = threading.Lock()
_DEFAULT_YAML_PATH = Path(os.environ.get("GLADOS_CONFIG_DIR", "/app/configs")) / "logging.yaml"


def get_registry() -> LogGroupRegistry:
    """Return the process-wide registry singleton.

    On first call, attempts to load from ``$GLADOS_CONFIG_DIR/logging.yaml``;
    falls back to builtin defaults if missing or invalid.
    """
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = LogGroupRegistry.from_yaml(_DEFAULT_YAML_PATH)
        return _REGISTRY


def reset_registry_for_tests() -> None:
    """Test hook — drops the singleton so each test gets a fresh registry."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


def install_loguru_sink(sink: Any | None = None, **add_kwargs: Any) -> int:
    """Replace loguru's existing sink(s) with a per-group filtered one.

    Returns the loguru handler ID so callers can remove it later if needed.
    Default sink is ``sys.stderr`` to match the previous global behaviour.

    Ordering: build (and possibly warn about) the registry FIRST, while
    loguru's default handler is still installed. Only then swap sinks.
    Otherwise startup warnings about a missing or corrupt
    ``configs/logging.yaml`` would emit to a removed handler.
    """
    import sys as _sys  # local import to avoid polluting module-level imports

    target_sink = sink if sink is not None else _sys.stderr

    # Force the registry to materialise NOW so any warnings reach the
    # default sink before we replace it.
    registry = get_registry()

    try:
        logger.remove()
    except ValueError:
        pass

    def _filter(record: dict[str, Any]) -> bool:
        gid = record["extra"].get("group")
        passed = registry.decide(gid, record["level"].no)
        if passed:
            registry.record_hit(gid)
        return passed

    # Sink at TRACE so every record reaches the filter; per-group thresholds
    # decide what actually emits.
    return logger.add(
        target_sink,
        level="TRACE",
        filter=_filter,
        **add_kwargs,
    )


def group_logger(group_id: str):
    """Return a loguru logger bound to ``group_id``.

    Usage::

        from glados.observability.log_groups import LogGroupId, group_logger
        log = group_logger(LogGroupId.CHAT.ROUND1_STREAM)
        log.info("upstream connecting: {}:{}", host, port)
    """
    return logger.bind(group=group_id)


__all__ = [
    "BUILTIN_GROUPS",
    "LogGroup",
    "LogGroupId",
    "LogGroupRegistry",
    "LogGroupsConfig",
    "LogLevel",
    "LOCKED_ON_GROUP_IDS",
    "get_registry",
    "group_logger",
    "install_loguru_sink",
    "reset_registry_for_tests",
]
