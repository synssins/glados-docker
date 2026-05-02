"""
Core engine module for the Glados voice assistant.

This module provides the main orchestration classes including the Glados assistant,
configuration management, and component coordination.
"""

from dataclasses import dataclass
from pathlib import Path
import os
import queue
import random
import sys
import threading
import time
from typing import Any, Callable, Literal

from loguru import logger
from pydantic import BaseModel, HttpUrl
import yaml

from ..ASR import TranscriberProtocol, get_audio_transcriber
from ..audio_io import AudioProtocol, get_audio_system
from ..audio_io.homeassistant_io import HomeAssistantAudioIO
from ..TTS import SpeechSynthesizerProtocol, get_speech_synthesizer
from ..utils import spoken_text_converter as stc
from ..utils.resources import resource_path
from ..autonomy import AutonomyConfig, AutonomyLoop, ConstitutionalState, EventBus, InteractionState, SubagentConfig, SubagentManager, TaskManager, TaskSlotStore
from ..autonomy.agents import CameraWatcherSubagent, CompactionAgent, EmotionAgent, HomeAssistantSensorSubagent, HackerNewsSubagent, ObserverAgent, WeatherSubagent
from ..autonomy.emotion_state import EmotionEvent
from ..autonomy.events import TimeTickEvent
from ..autonomy.llm_client import LLMConfig
from ..autonomy.summarization import estimate_tokens
from ..mcp import MCPManager, MCPServerConfig
from ..observability import MindRegistry, ObservabilityBus, trim_message
from ..vision import VisionConfig, VisionState
from ..vision.constants import SYSTEM_PROMPT_VISION_HANDLING
from .audio_data import AudioMessage
from .context import ContextBuilder
from .audio_state import AudioState
from .conversation_store import ConversationStore
from .knowledge_store import KnowledgeStore
from .llm_processor import LanguageModelProcessor
from .shutdown import ShutdownOrchestrator, ShutdownPriority
from .store import Store, format_preferences
from .llm_tracking import InFlightCounter
from .speech_listener import SpeechListener
from .buffered_speech_player import BufferedSpeechPlayer
from .speech_player import SpeechPlayer
from .text_listener import TextListener
from .tool_executor import ToolExecutor
from .tts_synthesizer import TextToSpeechSynthesizer

# Wire the per-group log filter (see glados/observability/log_groups.py).
# Replaces the prior single-sink hard-coded SUCCESS-floor — now each
# subsystem's group can be toggled / re-levelled at runtime via the WebUI
# Logging page or by editing configs/logging.yaml. The GLADOS_LOG_LEVEL
# env var still works as a global override floor for one-shot debugging.
from ..observability import install_loguru_sink as _install_loguru_sink  # noqa: E402

_install_loguru_sink(sys.stderr)


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: Callable[[list[str]], str]
    usage: str | None = None
    aliases: tuple[str, ...] = ()


class PersonalityPrompt(BaseModel):
    """
    Represents a single personality prompt message for the assistant.

    Contains exactly one of: system, user, or assistant message content.
    Used to configure the assistant's personality and behavior.
    """

    system: str | None = None
    user: str | None = None
    assistant: str | None = None

    def to_chat_message(self) -> dict[str, str]:
        """Convert the prompt to a chat message format.

        Returns:
            dict[str, str]: A single chat message dictionary

        Raises:
            ValueError: If the prompt does not contain exactly one non-null field
        """
        fields = self.model_dump(exclude_none=True)
        if len(fields) != 1:
            raise ValueError("PersonalityPrompt must have exactly one non-null field")

        field, value = next(iter(fields.items()))
        return {"role": field, "content": value}


class HAAudioConfig(BaseModel):
    """Configuration for the Home Assistant audio backend.

    Defaults are pulled from the centralized config store
    (``configs/global.yaml``) so there is a single source of truth
    for HA credentials, serve host/port, and audio paths.
    """

    ha_url: str = ""
    ha_token: str = ""
    media_player_entities: str | list[str] = ""
    serve_host: str = ""
    serve_port: int = 0
    serve_dir: str = ""

    def model_post_init(self, __context: Any) -> None:
        # Fill blanks from the centralized config store
        from .config_store import cfg
        if not self.ha_url:
            self.ha_url = cfg.ha_url
        if not self.ha_token:
            self.ha_token = cfg.ha_token
        if not self.media_player_entities:
            self.media_player_entities = cfg.speakers.default
        if not self.serve_host:
            self.serve_host = cfg.serve_host
        if not self.serve_port:
            self.serve_port = cfg.serve_port
        if not self.serve_dir:
            self.serve_dir = cfg.audio.ha_output_dir
        # Normalize single entity string to a list
        if isinstance(self.media_player_entities, str):
            self.media_player_entities = [self.media_player_entities]


def _ollama_as_chat_url(u: str | None) -> str:
    """Normalize a URL stored in services.yaml to the bare
    ``scheme://host:port`` form the engine stores in
    ``Glados.completion_url``.

    Mirrors ``_ollama_chat_url`` in ``glados/webui/tts_ui.py`` — same
    contract, duplicated deliberately to avoid an inbound import from
    webui into core. Stale path components on input (``/api/chat``,
    ``/v1/chat/completions``, ``/api/tags``, ``/v1/models``, anything
    else) are stripped; dispatch sites compose the right path at request
    time via ``glados.core.url_utils.compose_endpoint``."""
    from .url_utils import strip_url_path
    return strip_url_path(u)


def _reconcile_glados_with_services(glados_raw: Any) -> Any:
    """Override Glados-block llm_model / completion_url (and autonomy
    equivalents) from services.yaml whenever they disagree.

    services.llm_interactive and services.llm_autonomy are the
    UI's authoritative source (LLM & Services page). The Glados block
    historically duplicated these fields for engine convenience; the
    save-side sync (`glados/webui/tts_ui.py::_sync_glados_config_urls`)
    keeps them aligned on UI saves, but out-of-UI edits — e.g. a sed
    restore from a stale backup — would still leave the engine reading
    the drifted value at boot.

    This load-time reconciliation closes that gap: services.yaml wins,
    a warning names the override, and no path exists where the UI
    advertises one model while the engine runs another. Skipped if
    services.yaml is absent (dev/test without a configs dir) or the
    Glados block is malformed.
    """
    if not isinstance(glados_raw, dict):
        return glados_raw

    from .config_store import cfg  # local import to avoid cycle at module load

    services_yaml = cfg._configs_dir / "services.yaml"
    if not services_yaml.exists():
        return glados_raw

    try:
        svcs = cfg.services
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Services reconciliation skipped: {}", exc)
        return glados_raw

    chat_model = (svcs.llm_interactive.model or "").strip()
    chat_url = _ollama_as_chat_url(svcs.llm_interactive.url)
    auton_model = (svcs.llm_autonomy.model or "").strip()
    auton_url = _ollama_as_chat_url(svcs.llm_autonomy.url)

    if chat_model and glados_raw.get("llm_model") != chat_model:
        logger.warning(
            "Config drift: Glados.llm_model={!r} overridden by "
            "services.llm_interactive.model={!r} (UI is source of truth)",
            glados_raw.get("llm_model"), chat_model,
        )
        glados_raw["llm_model"] = chat_model

    if chat_url:
        current_chat = _ollama_as_chat_url(glados_raw.get("completion_url") or "")
        if current_chat != chat_url:
            logger.warning(
                "Config drift: Glados.completion_url={!r} overridden by "
                "services.llm_interactive.url={!r} (UI is source of truth)",
                glados_raw.get("completion_url"), chat_url,
            )
            glados_raw["completion_url"] = chat_url

    auton = glados_raw.get("autonomy")
    if isinstance(auton, dict):
        if auton_model and (auton.get("llm_model") or "") != auton_model:
            logger.warning(
                "Config drift: Glados.autonomy.llm_model={!r} overridden by "
                "services.llm_autonomy.model={!r} (UI is source of truth)",
                auton.get("llm_model"), auton_model,
            )
            auton["llm_model"] = auton_model
        if auton_url:
            current_auton = _ollama_as_chat_url(auton.get("completion_url") or "")
            if current_auton != auton_url:
                logger.warning(
                    "Config drift: Glados.autonomy.completion_url={!r} overridden by "
                    "services.llm_autonomy.url={!r} (UI is source of truth)",
                    auton.get("completion_url"), auton_url,
                )
                auton["completion_url"] = auton_url

    return glados_raw


class GladosConfig(BaseModel):
    """
    Configuration model for the Glados voice assistant.

    Defines all necessary parameters for initializing the assistant including
    LLM settings, audio I/O backend, ASR/TTS engines, and personality configuration.
    Supports loading from YAML files with nested key navigation.
    """

    llm_model: str
    completion_url: HttpUrl
    api_key: str | None
    interruptible: bool
    audio_io: str
    input_mode: Literal["audio", "text", "both"] = "audio"
    tts_enabled: bool = True
    asr_muted: bool = False
    asr_engine: str
    wake_word: str | None
    voice: str
    announcement: str | None
    llm_headers: dict[str, str] | None = None
    tui_theme: str | None = None
    personality_preprompt: list[PersonalityPrompt]
    slow_clap_audio_path: str = "data/slow-clap.mp3"
    tool_timeout: float = 30.0
    vision: VisionConfig | None = None
    autonomy: AutonomyConfig | None = None
    mcp_servers: list[MCPServerConfig] | None = None
    ha_audio: HAAudioConfig | None = None
    streaming_tts: bool = False
    streaming_tts_buffer_seconds: float = 3.0
    streaming_tts_chunk_chars: int = 80
    # First-TTS-flush threshold (chars). Smaller = earlier first audio
    # chunk at cost of one extra TTS call; larger = smoother continuous
    # playback. 30 is aggressive (first sentence fires ~200-300 ms into
    # LLM generation); 60-80 matches the rest of the stream.
    streaming_tts_first_chunk_chars: int = 30

    @classmethod
    def from_yaml(cls, path: str | Path, key_to_config: tuple[str, ...] = ("Glados",)) -> "GladosConfig":
        """
        Load a GladosConfig instance from a YAML configuration file.

        Parameters:
            path: Path to the YAML configuration file
            key_to_config: Tuple of keys to navigate nested configuration

        Returns:
            GladosConfig: Configuration object with validated settings

        Raises:
            ValueError: If the YAML content is invalid
            OSError: If the file cannot be read
            pydantic.ValidationError: If the configuration is invalid
        """
        path = Path(path)

        # Try different encodings
        for encoding in ["utf-8", "utf-8-sig"]:
            try:
                data = yaml.safe_load(path.read_text(encoding=encoding))
                break
            except UnicodeDecodeError:
                if encoding == "utf-8-sig":
                    raise ValueError(f"Could not decode YAML file {path} with any supported encoding")

        # Navigate through nested keys
        config = data
        for key in key_to_config:
            config = config[key]

        # Phase 8.13: reconcile with services.yaml before validation so
        # the UI's LLM & Services page stays the single source of truth
        # even if glados_config.yaml drifts via hand-edit or backup
        # restore. See docs/battery-findings-and-remediation-plan.md §8.13.
        config = _reconcile_glados_with_services(config)

        return cls.model_validate(config)

    def to_chat_messages(self) -> list[dict[str, str]]:
        """Convert personality preprompt to chat message format."""
        return [prompt.to_chat_message() for prompt in self.personality_preprompt]


def _maybe_discover_plugin_configs() -> list[MCPServerConfig]:
    """Discover plugins iff GLADOS_PLUGINS_ENABLED is truthy.

    Returns an empty list when disabled or when the discovery layer
    raises. Never propagates plugin-layer errors to the engine init.
    """
    enabled = os.environ.get("GLADOS_PLUGINS_ENABLED", "true").lower()
    if enabled not in ("1", "true", "yes", "on"):
        logger.info("Plugins disabled by GLADOS_PLUGINS_ENABLED env")
        return []

    plugin_mcp_configs: list[MCPServerConfig] = []
    try:
        from glados.plugins import discover_plugins, plugin_to_mcp_config
        for plugin in discover_plugins():
            try:
                plugin_mcp_configs.append(plugin_to_mcp_config(plugin))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Plugin {!s} failed to materialize MCP config; skipping: {}",
                    plugin.name, exc,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Plugin discovery layer failed; skipping: {}", exc)
    return plugin_mcp_configs


class Glados:
    """
    Glados voice assistant orchestrator.
    This class manages the components of the Glados voice assistant, including speech recognition,
    language model processing, text-to-speech synthesis, and audio playback.
    It initializes the necessary components, starts background threads for processing, and provides
    methods for interaction with the assistant.
    """

    PAUSE_TIME: float = 0.05  # Time to wait between processing loops
    NEUROTOXIN_RELEASE_ALLOWED: bool = False  # preparation for function calling, see issue #13
    DEFAULT_PERSONALITY_PREPROMPT: tuple[dict[str, str], ...] = (
        {
            "role": "system",
            "content": "You are a helpful AI assistant. You are here to assist the user in their tasks.",
        },
    )

    def __init__(
        self,
        asr_model: TranscriberProtocol,
        tts_model: SpeechSynthesizerProtocol,
        audio_io: AudioProtocol,
        completion_url: HttpUrl,
        llm_model: str,
        api_key: str | None = None,
        interruptible: bool = True,
        wake_word: str | None = None,
        announcement: str | None = None,
        personality_preprompt: tuple[dict[str, str], ...] = DEFAULT_PERSONALITY_PREPROMPT,
        tool_config: dict[str, Any] | None = None,
        tool_timeout: float = 30.0,
        vision_config: VisionConfig | None = None,
        autonomy_config: AutonomyConfig | None = None,
        mcp_servers: list[MCPServerConfig] | None = None,
        input_mode: Literal["audio", "text", "both"] = "audio",
        tts_enabled: bool = True,
        asr_muted: bool = False,
        llm_headers: dict[str, str] | None = None,
        streaming_tts: bool = False,
        streaming_tts_buffer_seconds: float = 3.0,
        streaming_tts_chunk_chars: int = 80,
        streaming_tts_first_chunk_chars: int = 30,
    ) -> None:
        """
        Initialize the Glados voice assistant with configuration parameters.

        This method sets up the voice recognition system, including voice activity detection (VAD),
        automatic speech recognition (ASR), text-to-speech (TTS), and language model processing.
        The initialization configures various components and starts background threads for
        processing LLM responses and TTS output.

        Args:
            asr_model (TranscriberProtocol): The ASR model for transcribing audio input.
            tts_model (SpeechSynthesizerProtocol): The TTS model for synthesizing spoken output.
            audio_io (AudioProtocol): The audio input/output system to use.
            completion_url (HttpUrl): The URL for the LLM completion endpoint.
            llm_model (str): The name of the LLM model to use.
            api_key (str | None): API key for accessing the LLM service, if required.
            interruptible (bool): Whether the assistant can be interrupted while speaking.
            wake_word (str | None): Optional wake word to trigger the assistant.
            announcement (str | None): Optional announcement to play on startup.
            personality_preprompt (tuple[dict[str, str], ...]): Initial personality preprompt messages.
            tool_config (dict[str, Any] | None): Configuration for tools (e.g., audio paths).
            tool_timeout (float): Timeout in seconds for tool execution.
            vision_config (VisionConfig | None): Optional vision configuration.
            autonomy_config (AutonomyConfig | None): Optional autonomy configuration.
            mcp_servers (list[MCPServerConfig] | None): Optional MCP server configurations.
            tts_enabled (bool): Whether TTS audio output is enabled at startup.
            asr_muted (bool): Whether ASR starts muted.
            llm_headers (dict[str, str] | None): Extra headers for LLM requests.
        """
        self._asr_model = asr_model
        self._tts = tts_model
        self.input_mode = input_mode
        self.completion_url = completion_url
        self.llm_model = llm_model
        self.api_key = api_key
        self.interruptible = interruptible
        self.wake_word = wake_word
        self.announcement = announcement
        self.tool_config = tool_config or {}
        self.tool_timeout = tool_timeout
        self.mcp_servers = mcp_servers or []
        self.robot_manager = None
        # Stage 3 Phase B: SQLite-backed conversation persistence so
        # history survives container restarts and Tier 1/2 exchanges
        # become context for subsequent Tier 3 calls. The DB is opened
        # under GLADOS_DATA (default /app/data/conversation.db). If
        # opening fails (disk read-only, permission, etc.), fall back
        # to in-memory only — the engine must keep running.
        try:
            from .conversation_db import ConversationDB
            _conv_db_path = os.path.join(
                os.environ.get("GLADOS_DATA", "/app/data"),
                "conversation.db",
            )
            self._conversation_db: ConversationDB | None = ConversationDB(_conv_db_path)
        except Exception as exc:
            logger.warning("ConversationDB init failed, in-memory only: {}", exc)
            self._conversation_db = None
        self._conversation_store = ConversationStore(
            initial_messages=list(personality_preprompt),
            db=self._conversation_db,
            conversation_id=os.environ.get("GLADOS_CONVERSATION_ID", "default"),
        )
        # Hydrate from disk so prior turns are visible on restart.
        # Limit to recent N to avoid replaying months of history into
        # every LLM call; compaction summaries below the limit still
        # live in ChromaDB and surface via RAG.
        if self._conversation_db is not None:
            try:
                _loaded = self._conversation_store.load_from_db(limit=200)
                if _loaded:
                    logger.info("ConversationStore hydrated with {} prior messages",
                                _loaded)
            except Exception as exc:
                logger.warning("ConversationStore hydrate failed: {}", exc)
        # Stage 3 Phase C+E: background retention sweeper. Reads policy
        # from cfg.memory; clamps max_days at the hard cap. Also enforces
        # episodic_ttl_hours on ChromaDB. Runs hourly by default. Failure
        # is non-fatal. The memory_store wiring is deferred — this runs
        # before the engine wires up self.memory_store, so we pass None
        # for now and the agent only does conversation-DB pruning until
        # _attach_memory_store_to_retention is called later in init.
        self._retention_agent = None
        if self._conversation_db is not None:
            try:
                from ..autonomy.agents.retention_agent import RetentionAgent
                from .config_store import cfg as _cfg_local
                _mem = _cfg_local.memory
                self._retention_agent = RetentionAgent(
                    self._conversation_db,
                    max_days=_mem.conversation_max_days,
                    hard_cap_days=_mem.conversation_hard_cap_days,
                    max_disk_mb=_mem.conversation_max_disk_mb,
                    sweep_interval_s=_mem.retention_sweep_interval_s,
                    episodic_ttl_hours=_mem.episodic_ttl_hours,
                )
                self._retention_agent.start()
            except Exception as exc:
                logger.warning("RetentionAgent init failed: {}", exc)
        self.vision_config = vision_config
        self.autonomy_config = autonomy_config or AutonomyConfig()
        self.vision_state: VisionState | None = VisionState() if self.vision_config else None
        self.vision_request_queue: queue.Queue | None = queue.Queue() if self.vision_config else None
        self.autonomy_event_bus: EventBus | None = None
        self.autonomy_loop: AutonomyLoop | None = None
        self.autonomy_slots: TaskSlotStore | None = None
        self.autonomy_tasks: TaskManager | None = None
        self.subagent_manager: SubagentManager | None = None
        self._emotion_agent: EmotionAgent | None = None
        self.constitutional_state = ConstitutionalState()
        self.observability_bus = ObservabilityBus()
        self.hub75_display = None  # Conditionally initialised after bus exists
        self.mind_registry = MindRegistry()
        self.interaction_state = InteractionState()
        self.asr_muted_event = threading.Event()
        if asr_muted:
            self.asr_muted_event.set()
        self.tts_muted_event = threading.Event()
        if not tts_enabled:
            self.tts_muted_event.set()
        self.streaming_tts = streaming_tts
        self.streaming_tts_buffer_seconds = streaming_tts_buffer_seconds
        # Phase 8.11: prefer AudioConfig knobs when they differ from
        # the legacy Glados-block defaults. AudioConfig is the new
        # source of truth per §0.2 (every operator knob on a WebUI
        # card); the Glados block is preserved for back-compat and
        # older YAML files.
        from glados.core.config_store import cfg as _p811_cfg
        _a = _p811_cfg.audio
        self.streaming_tts_chunk_chars = (
            _a.min_tts_flush_chars
            if _a.min_tts_flush_chars and _a.min_tts_flush_chars > 0
            else streaming_tts_chunk_chars
        )
        self.streaming_tts_first_chunk_chars = (
            _a.first_tts_flush_chars
            if _a.first_tts_flush_chars and _a.first_tts_flush_chars > 0
            else streaming_tts_first_chunk_chars
        )
        self._sentence_boundary_flush = bool(_a.sentence_boundary_flush)
        self.audio_state = AudioState()
        self.knowledge_store = KnowledgeStore(resource_path("data/knowledge.json"))
        self.preferences_store = Store[Any](
            path=resource_path("data/preferences.json"),
            formatter=format_preferences,
        )

        # Create unified context builder for LLM context injection
        self.context_builder = ContextBuilder()
        self.context_builder.register("preferences", self.preferences_store.as_prompt, priority=10)
        self.context_builder.register("knowledge", lambda: self._format_knowledge(), priority=5)
        self.context_builder.register("constitution", self.constitutional_state.get_modifiers_prompt, priority=3)

        # Register long-term memory — ChromaDB semantic search
        # Queries with current user message so only relevant facts are injected
        from ..memory import MemoryStore
        from .memory_context import MemoryContext, MemoryContextConfig
        from .config_store import cfg as _mem_cfg_store
        _mem_path = getattr(_mem_cfg_store.memory, "chromadb_path", "/app/data/chromadb")
        _mem_store: MemoryStore | None = None
        try:
            _mem_store = MemoryStore(persistent_path=_mem_path)
            _mem_store.health_check()
            logger.info("ChromaDB (embedded) memory store ready at {}", _mem_path)
        except Exception as exc:
            logger.warning("ChromaDB (embedded) unavailable — memory context disabled: {}", exc)
            _mem_store = None
        self.memory_store: MemoryStore | None = _mem_store
        # Stage 3 Phase E: now that memory_store exists, give the
        # already-running RetentionAgent a reference so its next
        # sweep can also enforce ChromaDB episodic_ttl_hours.
        if self._retention_agent is not None and _mem_store is not None:
            self._retention_agent._memory_store = _mem_store
        self.memory_context = MemoryContext(store=_mem_store, config=MemoryContextConfig())
        self.context_builder.register(
            "memory",
            lambda: self.memory_context.as_prompt(
                self.interaction_state.last_user_message if self.interaction_state else ""
            ),
            priority=7,
        )

        # Phase 8.14 — Portal canon RAG. Seed the semantic collection
        # from configs/canon/ on boot (idempotent via stable hashed ids
        # so re-runs do nothing), then register a gated context source
        # so canon injection fires only on turns that mention Portal
        # trigger keywords.
        from .canon_context import CanonContext, CanonContextConfig
        from .context_gates import needs_canon_context
        from ..memory.canon_loader import load_canon_from_configs
        self.canon_context = CanonContext(store=_mem_store, config=CanonContextConfig())
        if _mem_store is not None:
            try:
                loaded = load_canon_from_configs(_mem_store)
                if loaded:
                    total_added = sum(loaded.values())
                    if total_added:
                        logger.info(
                            "canon: seeded {} new entries across {} topic(s)",
                            total_added, len([t for t, n in loaded.items() if n]),
                        )
            except Exception as exc:
                logger.warning("canon: seeding failed at boot: {}", exc)

        def _canon_prompt_for_turn() -> str | None:
            msg = (
                self.interaction_state.last_user_message
                if self.interaction_state else ""
            )
            if not needs_canon_context(msg):
                return None
            return self.canon_context.as_prompt(msg)

        self.context_builder.register("canon", _canon_prompt_for_turn, priority=6)

        # Phase 8.x bugfix — chitchat / home-command guard per turn.
        # SSE builds these inline into its messages array; non-streaming
        # and voice paths go through the engine queue and need the same
        # guard injected via ContextBuilder or the 14B hallucinates
        # tool calls ("testing_tracks") or narrates fake device actions
        # on chitchat turns. Priority 7 = after canon/memory so the
        # guard sits closest to the user turn.
        def _turn_guard_for_turn() -> str | None:
            from glados.core.turn_guards import guard_for_message
            msg = (
                self.interaction_state.last_user_message
                if self.interaction_state else ""
            )
            if not msg:
                return None
            return guard_for_message(msg)

        self.context_builder.register(
            "turn_guard", _turn_guard_for_turn, priority=7,
        )

        # Load attitude directives for response variety
        from .attitude import load_attitudes
        personality_path = Path("configs/personality.yaml")
        attitudes_json_path = Path("configs/attitudes.json")
        if personality_path.exists():
            try:
                load_attitudes(personality_path)
            except Exception as exc:
                logger.warning(f"Failed to load personality config: {exc}")
        elif attitudes_json_path.exists():
            try:
                load_attitudes(attitudes_json_path)
            except Exception as exc:
                logger.warning(f"Failed to load attitudes config: {exc}")
        else:
            logger.warning("No personality/attitudes config found in configs/")

        # Initialize weather cache for LLM context injection
        from . import weather_cache
        from .context_gates import configure as _gates_configure, needs_weather_context
        weather_cache_path = self.autonomy_config.jobs.weather.weather_cache_path
        weather_cache.configure(weather_cache_path)
        # Configure context gates from YAML — add/remove keywords in configs/context_gates.yaml
        _gates_configure(Path("configs/context_gates.yaml"))

        # Initialize authoritative time source (NTP-synced, tz from
        # weather coords). Reads TimeGlobal from the config store and
        # passes weather_cache.get_data as the tz lookup callable so a
        # config reload that changes the weather location flows through
        # to time_source automatically. Background NTP sync starts
        # immediately and runs on the configured refresh interval.
        from . import time_source as _time_source
        from .config_store import cfg as _cfg_for_time
        try:
            _time_source.configure(
                _cfg_for_time.global_.time,
                weather_cache_getter=weather_cache.get_data,
            )
            _time_source.start()
        except Exception as _ts_exc:
            logger.warning("time_source init failed: {}", _ts_exc)
        # Weather context only injected when message is weather-related (saves ~200 tok/turn)
        self.context_builder.register(
            "weather",
            lambda: weather_cache.as_prompt() if needs_weather_context(
                self.interaction_state.last_user_message if self.interaction_state else ""
            ) else None,
            priority=2,
        )

        self._command_registry, self._command_order = self._build_command_registry()
        # Initialize events for thread synchronization
        self.processing_active_event = threading.Event()  # Indicates if input processing is active (ASR + LLM + TTS + VLM)
        self.currently_speaking_event = threading.Event()  # Indicates if the assistant is currently speaking
        self.shutdown_event = threading.Event()  # Event to signal shutdown of all threads

        # Initialize shutdown orchestrator for graceful shutdown
        self._shutdown_orchestrator = ShutdownOrchestrator(
            shutdown_event=self.shutdown_event,
            global_timeout=30.0,
            phase_timeout=10.0,
        )
        if self.autonomy_config.enabled:
            self.autonomy_event_bus = EventBus()
            self.autonomy_slots = TaskSlotStore(observability_bus=self.observability_bus)
            self.autonomy_tasks = TaskManager(self.autonomy_slots, self.autonomy_event_bus)
            # Register slots with context builder
            self.context_builder.register("slots", lambda: self._format_slots(), priority=8)
            if self.autonomy_config.jobs.enabled:
                self.subagent_manager = SubagentManager(
                    slot_store=self.autonomy_slots,
                    mind_registry=self.mind_registry,
                    observability_bus=self.observability_bus,
                    shutdown_event=self.shutdown_event,
                )
                # Create TTS queue early so subagents can use it for direct announcements
                self.tts_queue: queue.Queue[str] = queue.Queue()
                self._register_subagents()

        if self.vision_config:
            # Add instructions to system prompt to correctly handle [vision] marked messages
            messages = self._conversation_store.snapshot()
            vision_prompt_added = False
            for i, message in enumerate(messages):
                if message.get("role") == "system" and isinstance(message.get("content"), str):
                    self._conversation_store.modify_message(
                        i,
                        {"content": f"{message['content']} {SYSTEM_PROMPT_VISION_HANDLING}"}
                    )
                    vision_prompt_added = True
                    break
            if not vision_prompt_added:
                # Prepend a new system message with vision handling instructions
                current_messages = self._conversation_store.snapshot()
                self._conversation_store.replace_all(
                    [{"role": "system", "content": SYSTEM_PROMPT_VISION_HANDLING}] + current_messages
                )


        # Initialize spoken text converter, that converts text to spoken text. eg. 12 -> "twelve"
        # Phase 8.10: thread operator-editable pronunciation overrides
        # from cfg.tts_pronunciation so ``AI`` is read ``"Aye Eye"``
        # instead of the default all-caps splitter's slurred ``"A I"``.
        from glados.core.config_store import cfg as _pr_cfg
        _pr = _pr_cfg.tts_pronunciation
        self._stc = stc.SpokenTextConverter(
            symbol_expansions=dict(_pr.symbol_expansions),
            word_expansions=dict(_pr.word_expansions),
        )

        # warm up onnx ASR model, this is needed to avoid long pauses on first request
        self._asr_model.transcribe_file(resource_path("data/0.wav"))

        # Initialize queues for inter-thread communication
        self._autonomy_inflight = InFlightCounter()
        self.llm_queue_priority: queue.Queue[dict[str, Any]] = queue.Queue()
        autonomy_queue_max = self.autonomy_config.autonomy_queue_max
        autonomy_queue_size = autonomy_queue_max if autonomy_queue_max and autonomy_queue_max > 0 else 0
        self.llm_queue_autonomy: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=autonomy_queue_size)
        self.tool_calls_queue: queue.Queue[dict[str, Any]] = queue.Queue()  # Tool calls from LLMProcessor to ToolExecutor
        if not hasattr(self, "tts_queue"):
            self.tts_queue: queue.Queue[str] = queue.Queue()  # Text from LLMProcessor to TTSynthesizer
        self.audio_queue: queue.Queue[AudioMessage] = queue.Queue()  # AudioMessages from TTSSynthesizer to AudioPlayer

        # Merge config-driven MCP servers (services.yaml etc.) with
        # plugin-discovered MCP servers from /app/data/plugins/. Plugins
        # add to the catalog rather than replacing it — operators can
        # mix YAML-configured MCP servers with installed plugins.
        # See docs/plugins-architecture.md for the plugin design.
        plugin_mcp_configs = _maybe_discover_plugin_configs()

        all_mcp = list(self.mcp_servers or []) + plugin_mcp_configs

        self.mcp_manager: MCPManager | None = None
        if all_mcp:
            self.mcp_manager = MCPManager(
                all_mcp,
                tool_timeout=self.tool_timeout,
                observability_bus=self.observability_bus,
            )
            self.mcp_manager.start()

        # Initialize audio input/output system
        self.audio_io: AudioProtocol = audio_io
        logger.info("Audio I/O system initialized.")

        # Initialize threads for each component
        self.component_threads: list[threading.Thread] = []

        self.speech_listener: SpeechListener | None = None
        self.text_listener: TextListener | None = None
        if self.input_mode in {"audio", "both"}:
            self.speech_listener = SpeechListener(
                audio_io=self.audio_io,
                llm_queue=self.llm_queue_priority,
                asr_model=self._asr_model,
                wake_word=self.wake_word,
                interruptible=self.interruptible,
                shutdown_event=self.shutdown_event,
                currently_speaking_event=self.currently_speaking_event,
                processing_active_event=self.processing_active_event,
                pause_time=self.PAUSE_TIME,
                interaction_state=self.interaction_state,
                observability_bus=self.observability_bus,
                asr_muted_event=self.asr_muted_event,
                audio_state=self.audio_state,
                on_interrupt=lambda _: self._push_emotion_event("user", "User interrupted me mid-sentence"),
            )
        if self.input_mode in {"text", "both"}:
            if self.input_mode == "text":
                logger.info("Text input mode enabled. ASR is disabled.")
            self.text_listener = TextListener(
                llm_queue=self.llm_queue_priority,
                processing_active_event=self.processing_active_event,
                shutdown_event=self.shutdown_event,
                pause_time=self.PAUSE_TIME,
                interaction_state=self.interaction_state,
                observability_bus=self.observability_bus,
                command_handler=self.handle_command,
            )

        self.llm_processor = LanguageModelProcessor(
            llm_input_queue=self.llm_queue_priority,
            tool_calls_queue=self.tool_calls_queue,
            tts_input_queue=self.tts_queue,
            conversation_store=self._conversation_store,
            completion_url=self.completion_url,
            model_name=self.llm_model,
            api_key=self.api_key,
            processing_active_event=self.processing_active_event,
            shutdown_event=self.shutdown_event,
            pause_time=self.PAUSE_TIME,
            vision_state=self.vision_state,
            slot_store=self.autonomy_slots,
            preferences_store=self.preferences_store,
            constitutional_state=self.constitutional_state,
            context_builder=self.context_builder,
            autonomy_system_prompt=self.autonomy_config.system_prompt if self.autonomy_config.enabled else None,
            mcp_manager=self.mcp_manager,
            observability_bus=self.observability_bus,
            extra_headers=llm_headers,
            lane="priority",
            streaming_tts_chunk_chars=self.streaming_tts_chunk_chars if self.streaming_tts else None,
            streaming_tts_first_chunk_chars=self.streaming_tts_first_chunk_chars if self.streaming_tts else None,
            sentence_boundary_flush=self._sentence_boundary_flush,
        )
        self.autonomy_llm_processors: list[LanguageModelProcessor] = []
        autonomy_parallel_calls = 0
        if self.autonomy_config.enabled:
            autonomy_parallel_calls = max(0, self.autonomy_config.autonomy_parallel_calls)
        # Autonomy may use a different GPU/endpoint than interactive chat
        _autonomy_url = self.autonomy_config.completion_url or self.completion_url
        _autonomy_model = self.autonomy_config.llm_model or self.llm_model
        for _ in range(autonomy_parallel_calls):
            self.autonomy_llm_processors.append(
                LanguageModelProcessor(
                    llm_input_queue=self.llm_queue_autonomy,
                    tool_calls_queue=self.tool_calls_queue,
                    tts_input_queue=self.tts_queue,
                    conversation_store=self._conversation_store,
                    completion_url=_autonomy_url,
                    model_name=_autonomy_model,
                    api_key=self.api_key,
                    processing_active_event=self.processing_active_event,
                    shutdown_event=self.shutdown_event,
                    pause_time=self.PAUSE_TIME,
                    vision_state=self.vision_state,
                    slot_store=self.autonomy_slots,
                    preferences_store=self.preferences_store,
                    constitutional_state=self.constitutional_state,
                    context_builder=self.context_builder,
                    autonomy_system_prompt=self.autonomy_config.system_prompt if self.autonomy_config.enabled else None,
                    mcp_manager=self.mcp_manager,
                    observability_bus=self.observability_bus,
                    extra_headers=llm_headers,
                    lane="autonomy",
                    inflight_counter=self._autonomy_inflight,
                    streaming_tts_chunk_chars=self.streaming_tts_chunk_chars if self.streaming_tts else None,
                    streaming_tts_first_chunk_chars=self.streaming_tts_first_chunk_chars if self.streaming_tts else None,
                    sentence_boundary_flush=self._sentence_boundary_flush,
                )
            )

        # Conditionally start robot manager
        try:
            from glados.core.config_store import cfg as _cfg
            if _cfg.robots.enabled:
                from glados.robots.manager import RobotManager
                self.robot_manager = RobotManager(_cfg.robots)
                self.robot_manager.start()
                logger.info("Robot manager started from engine")
        except Exception as e:
            logger.warning("Robot manager init skipped: {}", e)

        self.tool_executor = ToolExecutor(
            llm_queue_priority=self.llm_queue_priority,
            llm_queue_autonomy=self.llm_queue_autonomy,
            tool_calls_queue=self.tool_calls_queue,
            processing_active_event=self.processing_active_event,
            shutdown_event=self.shutdown_event,
            tool_config={
                **self.tool_config,
                "vision_request_queue": self.vision_request_queue,
                "vision_tool_timeout": self.tool_timeout,
                "tts_queue": self.tts_queue,
                "preferences_store": self.preferences_store,
                "slot_store": self.autonomy_slots,
                "robot_manager": self.robot_manager,
            },
            tool_timeout=self.tool_timeout,
            pause_time=self.PAUSE_TIME,
            mcp_manager=self.mcp_manager,
            observability_bus=self.observability_bus,
            on_tool_event=self._on_tool_event,
        )

        self.tts_synthesizer = TextToSpeechSynthesizer(
            tts_input_queue=self.tts_queue,
            audio_output_queue=self.audio_queue,
            tts_model=self._tts,
            stc_instance=self._stc,
            shutdown_event=self.shutdown_event,
            pause_time=self.PAUSE_TIME,
            tts_muted_event=self.tts_muted_event,
            observability_bus=self.observability_bus,
        )

        if self.streaming_tts:
            logger.info(
                "Streaming TTS enabled (buffer={:.1f}s, chunk={}chars)",
                self.streaming_tts_buffer_seconds,
                self.streaming_tts_chunk_chars,
            )
            self.speech_player = BufferedSpeechPlayer(
                audio_io=self.audio_io,
                audio_output_queue=self.audio_queue,
                conversation_store=self._conversation_store,
                tts_sample_rate=self._tts.sample_rate,
                shutdown_event=self.shutdown_event,
                currently_speaking_event=self.currently_speaking_event,
                processing_active_event=self.processing_active_event,
                pause_time=self.PAUSE_TIME,
                buffer_seconds=self.streaming_tts_buffer_seconds,
                tts_muted_event=self.tts_muted_event,
                interaction_state=self.interaction_state,
                observability_bus=self.observability_bus,
            )
        else:
            self.speech_player = SpeechPlayer(
                audio_io=self.audio_io,
                audio_output_queue=self.audio_queue,
                conversation_store=self._conversation_store,
                tts_sample_rate=self._tts.sample_rate,
                shutdown_event=self.shutdown_event,
                currently_speaking_event=self.currently_speaking_event,
                processing_active_event=self.processing_active_event,
                pause_time=self.PAUSE_TIME,
                tts_muted_event=self.tts_muted_event,
                interaction_state=self.interaction_state,
                observability_bus=self.observability_bus,
            )

        self.vision_processor = None
        if self.vision_config:
            from ..vision import VisionProcessor
            self.vision_processor = VisionProcessor(
                vision_state=self.vision_state,
                processing_active_event=self.processing_active_event,
                shutdown_event=self.shutdown_event,
                config=self.vision_config,
                request_queue=self.vision_request_queue,
                event_bus=self.autonomy_event_bus,
                observability_bus=self.observability_bus,
            )

        self.autonomy_ticker_thread: threading.Thread | None = None
        if self.autonomy_config.enabled:
            assert self.autonomy_event_bus is not None
            assert self.autonomy_slots is not None
            self.autonomy_loop = AutonomyLoop(
                config=self.autonomy_config,
                event_bus=self.autonomy_event_bus,
                interaction_state=self.interaction_state,
                vision_state=self.vision_state,
                slot_store=self.autonomy_slots,
                llm_queue=self.llm_queue_autonomy,
                processing_active_event=self.processing_active_event,
                currently_speaking_event=self.currently_speaking_event,
                shutdown_event=self.shutdown_event,
                observability_bus=self.observability_bus,
                inflight_counter=self._autonomy_inflight,
                pause_time=self.PAUSE_TIME,
            )
            if not self.vision_config:
                self.autonomy_ticker_thread = threading.Thread(
                    target=self._run_autonomy_ticker,
                    name="AutonomyTicker",
                    daemon=True,
                )

        # Define thread configurations with daemon settings and shutdown priorities
        # daemon=True: Can be killed without waiting (pure input, stateless)
        # daemon=False: Must be joined (has in-flight state to preserve)
        thread_configs: dict[str, tuple[Any, bool, ShutdownPriority, queue.Queue | None]] = {
            "LLMProcessor": (
                self.llm_processor.run,
                False,  # Has in-flight conversation updates
                ShutdownPriority.PROCESSING,
                self.llm_queue_priority,
            ),
            "ToolExecutor": (
                self.tool_executor.run,
                False,  # Tool results need to be recorded
                ShutdownPriority.PROCESSING,
                self.tool_calls_queue,
            ),
            "TTSSynthesizer": (
                self.tts_synthesizer.run,
                False,  # Pending TTS to complete
                ShutdownPriority.OUTPUT,
                self.tts_queue,
            ),
            "AudioPlayer": (
                self.speech_player.run,
                False,  # Audio playing needs to finish
                ShutdownPriority.OUTPUT,
                self.audio_queue,
            ),
        }
        for index, processor in enumerate(self.autonomy_llm_processors, start=1):
            thread_configs[f"LLMProcessorAutonomy-{index}"] = (
                processor.run,
                False,  # Has in-flight conversation updates
                ShutdownPriority.PROCESSING,
                self.llm_queue_autonomy,
            )
        if self.speech_listener:
            thread_configs["SpeechListener"] = (
                self.speech_listener.run,
                True,  # Pure input, no state
                ShutdownPriority.INPUT,
                None,
            )
        if self.text_listener:
            thread_configs["TextListener"] = (
                self.text_listener.run,
                True,  # Pure input, no state
                ShutdownPriority.INPUT,
                None,
            )
        if self.autonomy_loop:
            thread_configs["AutonomyLoop"] = (
                self.autonomy_loop.run,
                True,  # Can safely abandon
                ShutdownPriority.BACKGROUND,
                None,
            )
        if self.vision_processor:
            thread_configs["VisionProcessor"] = (
                self.vision_processor.run,
                True,  # Can safely abandon
                ShutdownPriority.BACKGROUND,
                self.vision_request_queue,
            )
        if self.autonomy_ticker_thread:
            self.component_threads.append(self.autonomy_ticker_thread)
            self.autonomy_ticker_thread.start()
            self._shutdown_orchestrator.register(
                "AutonomyTicker",
                self.autonomy_ticker_thread,
                priority=ShutdownPriority.BACKGROUND,
            )
            logger.info("Orchestrator: AutonomyTicker thread started.")
            self.mind_registry.register(
                "AutonomyTicker",
                title="Autonomy Ticker",
                status="running",
                summary="Periodic autonomy ticks",
            )

        for name in thread_configs:
            self.mind_registry.register(name, title=name, status="starting", summary="Initializing")

        for name, (target_func, daemon, priority, component_queue) in thread_configs.items():
            thread = threading.Thread(target=target_func, name=name, daemon=daemon)
            self.component_threads.append(thread)
            thread.start()
            self._shutdown_orchestrator.register(
                name,
                thread,
                queue=component_queue,
                priority=priority,
            )
            logger.info(f"Orchestrator: {name} thread started (daemon={daemon}).")
            self.mind_registry.update(name, "running", summary="Thread active")

        # Start subagents after other components are running
        if self.subagent_manager:
            self.subagent_manager.start_all()

        # Start HUB75 display (bus consumer — starts after all producers)
        # Wrapped in try/except so a WLED timeout can never crash the engine.
        from ..core.config_store import cfg as _cfg
        if _cfg.hub75.enabled:
            try:
                from ..hub75 import Hub75Display
                self.hub75_display = Hub75Display(
                    observability_bus=self.observability_bus,
                    config=_cfg.hub75,
                )
                self.hub75_display.start()
                logger.success("HUB75 display started ({}:{})",
                               _cfg.hub75.wled_ip, _cfg.hub75.wled_ddp_port)
            except Exception as exc:
                logger.warning("HUB75: failed to start — engine continues without display: {}", exc)
                self.hub75_display = None

    def _register_subagents(self) -> None:
        """Register configured subagents with the manager."""
        if not self.subagent_manager:
            return

        jobs_config = self.autonomy_config.jobs

        # Create shared LLM config for subagents — use autonomy GPU if configured
        _sub_url = self.autonomy_config.completion_url or str(self.completion_url)
        _sub_model = self.autonomy_config.llm_model or self.llm_model
        llm_config = LLMConfig(
            url=str(_sub_url),
            api_key=self.api_key,
            model=_sub_model,
            timeout=180.0,
        )
        # Expose for passive memory extraction (Option A framework)
        self._autonomy_llm_config = llm_config

        if jobs_config.hacker_news.enabled:
            hn_config = SubagentConfig(
                agent_id="hn_top",
                title="Hacker News",
                role="news_monitor",
                loop_interval_s=jobs_config.hacker_news.interval_s,
                run_on_start=True,
            )
            hn_subagent = HackerNewsSubagent(
                config=hn_config,
                top_n=jobs_config.hacker_news.top_n,
                min_score=jobs_config.hacker_news.min_score,
                llm_config=llm_config,
                slot_store=self.autonomy_slots,
                mind_registry=self.mind_registry,
                observability_bus=self.observability_bus,
                shutdown_event=self.shutdown_event,
            )
            self.subagent_manager.register(hn_subagent)

        if jobs_config.weather.enabled:
            if jobs_config.weather.latitude is None or jobs_config.weather.longitude is None:
                logger.warning("Weather subagent enabled but latitude/longitude are missing.")
            else:
                weather_config = SubagentConfig(
                    agent_id="weather",
                    title="Weather",
                    role="weather_monitor",
                    loop_interval_s=jobs_config.weather.interval_s,
                    run_on_start=True,
                )
                weather_subagent = WeatherSubagent(
                    config=weather_config,
                    weather_config=jobs_config.weather,
                    llm_config=llm_config,
                    slot_store=self.autonomy_slots,
                    mind_registry=self.mind_registry,
                    observability_bus=self.observability_bus,
                    shutdown_event=self.shutdown_event,
                )
                self.subagent_manager.register(weather_subagent)

        if jobs_config.camera_watcher.enabled:
            camera_config = SubagentConfig(
                agent_id="camera_watcher",
                title="Camera Watcher",
                role="security_monitor",
                loop_interval_s=jobs_config.camera_watcher.interval_s,
                run_on_start=True,
            )
            camera_subagent = CameraWatcherSubagent(
                config=camera_config,
                vision_api_url=jobs_config.camera_watcher.vision_api_url,
                slot_store=self.autonomy_slots,
                mind_registry=self.mind_registry,
                observability_bus=self.observability_bus,
                shutdown_event=self.shutdown_event,
            )
            self.subagent_manager.register(camera_subagent)

        if jobs_config.ha_sensor.enabled:
            ha_sensor_config = SubagentConfig(
                agent_id="ha_sensor",
                title="HA Sensor Watcher",
                role="home_monitor",
                loop_interval_s=jobs_config.ha_sensor.interval_s,
                run_on_start=True,
            )
            ha_sensor_subagent = HomeAssistantSensorSubagent(
                config=ha_sensor_config,
                ha_ws_url=jobs_config.ha_sensor.ha_ws_url,
                ha_token=jobs_config.ha_sensor.ha_token,
                entity_categories=jobs_config.ha_sensor.entity_categories,
                debounce_seconds=jobs_config.ha_sensor.debounce_seconds,
                min_importance=jobs_config.ha_sensor.min_importance,
                tts_queue=self.tts_queue,
                vision_api_url=jobs_config.ha_sensor.vision_api_url,
                vision_entities=jobs_config.ha_sensor.vision_entities,
                smart_detection=jobs_config.ha_sensor.smart_detection,
                pet_outdoor_monitor=jobs_config.ha_sensor.pet_outdoor_monitor or None,
                mode_change_callback=self._on_mode_change,
                slot_store=self.autonomy_slots,
                mind_registry=self.mind_registry,
                observability_bus=self.observability_bus,
                shutdown_event=self.shutdown_event,
            )
            self.subagent_manager.register(ha_sensor_subagent)

        # Emotion agent - always registered, core to GLaDOS personality
        emotion_cfg = self.autonomy_config.emotion
        emotion_subagent_config = SubagentConfig(
            agent_id="emotion",
            title="Emotional State",
            role="emotional_regulation",
            loop_interval_s=emotion_cfg.tick_interval_s,
            run_on_start=True,
        )
        emotion_agent = EmotionAgent(
            config=emotion_subagent_config,
            llm_config=llm_config,
            emotion_config=emotion_cfg,
            constitutional_state=self.constitutional_state,  # Bridge wiring
            slot_store=self.autonomy_slots,
            mind_registry=self.mind_registry,
            observability_bus=self.observability_bus,
            shutdown_event=self.shutdown_event,
        )
        self.subagent_manager.register(emotion_agent)
        self._emotion_agent = emotion_agent  # Keep reference for event pushing
        # Wire emotion state into context builder so LLM sees current emotional state
        self.context_builder.register(
            "emotion",
            lambda: emotion_agent.state.to_prompt() if emotion_agent.state else None,
            priority=6,  # Between memory (7) and knowledge (5)
        )
        # Wire emotion agent to autonomy loop for vision events
        if self.autonomy_config.enabled and getattr(self, "autonomy_loop", None) is not None:
            self.autonomy_loop.set_emotion_agent(emotion_agent)

        # Compaction agent - monitors conversation size and compacts when needed
        compaction_config = SubagentConfig(
            agent_id="compaction",
            title="Message Compaction",
            role="context_management",
            loop_interval_s=60.0,  # Check every minute
            run_on_start=False,  # Wait for conversation to build up
        )
        compaction_agent = CompactionAgent(
            config=compaction_config,
            llm_config=llm_config,
            conversation_store=self._conversation_store,
            token_threshold=self.autonomy_config.tokens.token_threshold,
            preserve_recent=self.autonomy_config.tokens.preserve_recent_messages,
            memory_store=self.memory_store,  # Write extracted facts to ChromaDB
            slot_store=self.autonomy_slots,
            mind_registry=self.mind_registry,
            observability_bus=self.observability_bus,
            shutdown_event=self.shutdown_event,
        )
        self.subagent_manager.register(compaction_agent)

        # Observer agent - monitors behavior and proposes adjustments
        observer_config = SubagentConfig(
            agent_id="observer",
            title="Behavior Observer",
            role="meta_supervision",
            loop_interval_s=300.0,  # Analyze every 5 minutes
            run_on_start=False,  # Wait for conversation to build up
        )
        observer_agent = ObserverAgent(
            config=observer_config,
            llm_config=llm_config,
            conversation_store=self._conversation_store,
            constitutional_state=self.constitutional_state,
            sample_count=10,
            min_samples_for_analysis=5,
            slot_store=self.autonomy_slots,
            mind_registry=self.mind_registry,
            observability_bus=self.observability_bus,
            shutdown_event=self.shutdown_event,
        )
        self.subagent_manager.register(observer_agent)

    # ------------------------------------------------------------------
    # Mode change callback (called by HA sensor watcher)
    # ------------------------------------------------------------------

    def _on_mode_change(
        self,
        maintenance_mode: bool,
        maintenance_speaker: str,
        silent_mode: bool,
    ) -> None:
        """React to maintenance/silent mode HA entity changes.

        Called by :class:`HomeAssistantSensorSubagent` whenever one of the
        three mode helper entities changes state.  Propagates the new mode
        to :class:`HomeAssistantAudioIO` so that *all* engine-routed audio
        (conversation TTS, startup announcements, etc.) respects the mode.
        """
        if isinstance(self.audio_io, HomeAssistantAudioIO):
            self.audio_io.silent_mode = silent_mode
            if not silent_mode and maintenance_mode and maintenance_speaker:
                self.audio_io.maintenance_speaker = maintenance_speaker
            elif not silent_mode and not maintenance_mode:
                self.audio_io.maintenance_speaker = None
            # If silent_mode is on, speaker routing doesn't matter (audio suppressed)

        logger.success(
            "Mode change: maintenance={} (speaker={}), silent={}",
            maintenance_mode,
            maintenance_speaker or "(none)",
            silent_mode,
        )

    # Pre-generated startup WAVs directory — driven by config/env
    STARTUP_AUDIO_DIR = Path(
        os.environ.get("GLADOS_AUDIO", "/app/audio_files")
    ) / "glados_announcements" / "startup"
    # Fallback text if no pre-generated WAVs exist yet
    _DEFAULT_ANNOUNCEMENT = "All systems nominal. Not that anyone asked."

    def _resolve_startup_speaker(self, ha_url: str, ha_token: str) -> list[str] | None:
        """Query HA for maintenance mode state before threads have caught up.

        Returns the maintenance speaker entity as a list if maintenance mode
        is active, or None to use the default audio_io entities.
        """
        import httpx

        try:
            headers = {"Authorization": f"Bearer {ha_token}"}
            # Check maintenance mode boolean
            resp = httpx.get(
                f"{ha_url}/api/states/input_boolean.glados_maintenance_mode",
                headers=headers,
                timeout=5.0,
            )
            resp.raise_for_status()
            if resp.json().get("state") != "on":
                return None  # not in maintenance — use defaults

            # Check silent mode — if silent, skip audio entirely
            resp_silent = httpx.get(
                f"{ha_url}/api/states/input_boolean.glados_silent_mode",
                headers=headers,
                timeout=5.0,
            )
            resp_silent.raise_for_status()
            if resp_silent.json().get("state") == "on":
                logger.info("Startup announcement suppressed — silent mode active")
                return []  # empty list = skip playback

            # Get maintenance speaker
            resp_spk = httpx.get(
                f"{ha_url}/api/states/input_text.glados_maintenance_speaker",
                headers=headers,
                timeout=5.0,
            )
            resp_spk.raise_for_status()
            speaker = resp_spk.json().get("state", "").strip()
            if speaker:
                logger.success("Startup announcement routed to maintenance speaker: {}", speaker)
                return [speaker]

            logger.warning("Maintenance mode active but no speaker set — using defaults")
            return None
        except Exception as exc:
            logger.warning("Could not query HA for maintenance state: {}", exc)
            return None

    def play_announcement(self, interruptible: bool | None = None) -> None:
        """
        Play a randomized pre-generated GLaDOS startup announcement.

        Picks a random WAV from glados_announcements/startup/ and plays it
        directly via HA, bypassing TTS synthesis for instant startup audio.
        Falls back to TTS queue if no pre-generated WAVs are available.

        Queries HA directly for maintenance/silent mode state to resolve the
        correct speaker, since the HA sensor watcher thread may not have
        fetched initial state yet at boot time.

        Args:
            interruptible (bool | None): Whether the announcement can be interrupted.
                If `None`, defaults to the instance's `interruptible` setting.
        """
        if interruptible is None:
            interruptible = self.interruptible

        wav_files = sorted(self.STARTUP_AUDIO_DIR.glob("startup_*.wav")) if self.STARTUP_AUDIO_DIR.exists() else []

        if wav_files:
            chosen = random.choice(wav_files)
            logger.success("Playing startup announcement (pre-generated): {}", chosen.name)
            # Copy to HA serve directory and play directly — no TTS needed
            import shutil
            serve_dir = Path(
                os.environ.get("GLADOS_AUDIO", "/app/audio_files")
            ) / "glados_ha"
            serve_dir.mkdir(parents=True, exist_ok=True)
            dest = serve_dir / chosen.name
            shutil.copy2(chosen, dest)
            # Play via HA media_player — query HA directly for maintenance state
            try:
                import httpx
                ha_url = getattr(self.audio_io, "ha_url", None)
                ha_token = getattr(self.audio_io, "ha_token", None)
                serve_host = getattr(self.audio_io, "serve_host", None)
                serve_port = getattr(self.audio_io, "serve_port", 5051)
                if not ha_url or not ha_token or not serve_host:
                    # No fallback — audio_io must be configured with real
                    # credentials. Committing defaults is a security leak
                    # (2026-04-23 incident: a real long-lived HA token
                    # sat in this file for 11 days).
                    logger.warning(
                        "startup-audio: ha_url / ha_token / serve_host "
                        "missing from audio_io; skipping HA playback"
                    )
                    return

                # Resolve speaker: check HA maintenance state directly (beats the race)
                override = self._resolve_startup_speaker(ha_url, ha_token)
                if override is not None and len(override) == 0:
                    return  # silent mode — skip entirely
                entities = override or getattr(self.audio_io, "media_player_entities", [])
                if not entities:
                    logger.warning(
                        "startup-audio: no media_player entities configured; skipping"
                    )
                    return

                from glados.core.tls import is_tls_active
                _proto = "https" if is_tls_active() else "http"
                media_url = f"{_proto}://{serve_host}:{serve_port}/{chosen.name}"
                httpx.post(
                    f"{ha_url}/api/services/media_player/play_media",
                    headers={"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"},
                    json={
                        "entity_id": entities,
                        "media_content_id": media_url,
                        "media_content_type": "music",
                    },
                    timeout=10.0,
                )
            except Exception as e:
                logger.warning("Startup announcement HA playback failed, falling back to TTS: {}", e)
                self.tts_queue.put(self._DEFAULT_ANNOUNCEMENT)
                self.processing_active_event.set()
        else:
            # No pre-generated WAVs yet — fall back to TTS
            logger.warning("No pre-generated startup WAVs found in {}, using TTS fallback", self.STARTUP_AUDIO_DIR)
            announcements_path = Path(__file__).resolve().parents[3] / "configs" / "startup_announcements.txt"
            try:
                lines = [l.strip() for l in announcements_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            except Exception:
                lines = [self._DEFAULT_ANNOUNCEMENT]
            line = random.choice(lines) if lines else self._DEFAULT_ANNOUNCEMENT
            logger.success("Playing startup announcement (TTS): {}", line)
            self.tts_queue.put(line)
            self.processing_active_event.set()

    @property
    def messages(self) -> list[dict[str, Any]]:
        """
        Retrieve the current list of conversation messages.

        Returns:
            list[dict[str, Any]]: A snapshot of message dictionaries representing the conversation history.
        """
        return self._conversation_store.snapshot()

    @classmethod
    def from_config(cls, config: GladosConfig) -> "Glados":
        """
        Create a Glados instance from a GladosConfig configuration object.

        Parameters:
            config (GladosConfig): Configuration object containing Glados initialization parameters

        Returns:
            Glados: A new Glados instance configured with the provided settings
        """

        asr_model = get_audio_transcriber(
            engine_type=config.asr_engine,
        )

        tts_model: SpeechSynthesizerProtocol
        tts_model = get_speech_synthesizer(config.voice)

        ha_config_dict = config.ha_audio.model_dump() if config.ha_audio else None

        # NOTE: maintenance/silent mode is now dynamic — driven by HA helper
        # entities (input_boolean.glados_maintenance_mode, etc.) and applied
        # at runtime via _on_mode_change() callback from the HA sensor watcher.

        audio_io = get_audio_system(
            backend_type=config.audio_io,
            ha_config=ha_config_dict,
        )

        return cls(
            asr_model=asr_model,
            tts_model=tts_model,
            audio_io=audio_io,
            completion_url=config.completion_url,
            llm_model=config.llm_model,
            api_key=config.api_key,
            interruptible=config.interruptible,
            wake_word=config.wake_word,
            announcement=config.announcement,
            personality_preprompt=tuple(config.to_chat_messages()),
            tool_config={"slow_clap_audio_path": config.slow_clap_audio_path},
            tool_timeout=config.tool_timeout,
            vision_config=config.vision,
            autonomy_config=config.autonomy,
            mcp_servers=config.mcp_servers,
            input_mode=config.input_mode,
            tts_enabled=config.tts_enabled,
            asr_muted=config.asr_muted,
            llm_headers=config.llm_headers,
            streaming_tts=config.streaming_tts,
            streaming_tts_buffer_seconds=config.streaming_tts_buffer_seconds,
            streaming_tts_chunk_chars=config.streaming_tts_chunk_chars,
            streaming_tts_first_chunk_chars=config.streaming_tts_first_chunk_chars,
        )

    @classmethod
    def from_yaml(cls, path: str) -> "Glados":
        """
        Create a Glados instance from a configuration file.

        Parameters:
            path (str): Path to the YAML configuration file containing Glados settings.

        Returns:
            Glados: A new Glados instance configured with settings from the specified YAML file.

        Example:
            glados = Glados.from_yaml('config/default.yaml')
        """
        return cls.from_config(GladosConfig.from_yaml(path))

    def run(self) -> None:
        """
        Start the voice assistant's listening event loop, continuously processing audio input.
        This method initializes the audio input system, starts listening for audio samples,
        and enters a loop that waits for audio input until a shutdown event is triggered.
        It handles keyboard interrupts gracefully and ensures that all components are properly shut down.

        This method is the main entry point for running the Glados voice assistant.
        """
        if self.input_mode in {"audio", "both"}:
            try:
                self.audio_io.start_listening()
                logger.success("Audio input stream started successfully")
            except RuntimeError as e:
                logger.error(f"Failed to start audio input: {e}")
                logger.warning("Voice input disabled - text input still available")
        else:
            logger.info("Text input mode active. Audio input is disabled.")

        logger.success("Engine running")
        logger.success("Listening...")

        # Loop forever, but is 'paused' when new samples are not available
        try:
            while not self.shutdown_event.is_set():  # Check event BEFORE blocking get
                time.sleep(self.PAUSE_TIME)
            logger.info("Shutdown event detected in listen loop, exiting loop.")

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt in main run loop.")
            # Make sure any ongoing audio playback is stopped
            if self.currently_speaking_event.is_set():
                for component in self.component_threads:
                    if component.name == "AudioPlayer":
                        self.audio_io.stop_speaking()
                        self.currently_speaking_event.clear()
                        break
        finally:
            self._graceful_shutdown()

    def _graceful_shutdown(self) -> None:
        """Perform graceful shutdown of all components."""
        logger.info("Beginning graceful shutdown...")

        # Stop robot manager
        if self.robot_manager is not None:
            logger.debug("Shutting down robot manager...")
            self.robot_manager.stop()

        # Stop HUB75 display (blanks panel before other components shut down)
        if self.hub75_display is not None:
            logger.debug("Shutting down HUB75 display...")
            self.hub75_display.stop()

        # Stop subagents first (they may be using shared resources)
        if self.subagent_manager:
            logger.debug("Shutting down subagent manager...")
            self.subagent_manager.shutdown(timeout=5.0)

        # Stop task manager
        if self.autonomy_tasks:
            logger.debug("Shutting down task manager...")
            self.autonomy_tasks.shutdown(wait=True)

        # Use orchestrator for coordinated thread shutdown
        results = self._shutdown_orchestrator.initiate_shutdown()

        # Update mind registry for all components
        for component in self.component_threads:
            self.mind_registry.update(component.name, "stopped", summary="Shutdown")
        if self.autonomy_ticker_thread:
            self.mind_registry.update("AutonomyTicker", "stopped", summary="Shutdown")

        # Stop MCP manager last (other components may need it during shutdown)
        if self.mcp_manager:
            logger.debug("Shutting down MCP manager...")
            self.mcp_manager.shutdown()

        # Log any failed shutdowns
        failed = [r for r in results if not r.success]
        if failed:
            logger.warning(
                "Some components did not shut down cleanly: {}",
                [r.component for r in failed],
            )

        logger.info("Graceful shutdown complete.")

    def set_asr_muted(self, muted: bool) -> None:
        if muted:
            self.asr_muted_event.set()
        else:
            self.asr_muted_event.clear()
        if self.speech_listener:
            self.speech_listener.reset()
        self.audio_state.reset()
        if self.observability_bus:
            state = "muted" if muted else "unmuted"
            self.observability_bus.emit(
                source="asr",
                kind="mute",
                message=f"ASR {state}",
                meta={"muted": muted},
            )

    def toggle_asr_muted(self) -> bool:
        muted = not self.asr_muted_event.is_set()
        self.set_asr_muted(muted)
        return muted

    def set_tts_muted(self, muted: bool) -> None:
        if muted:
            self.tts_muted_event.set()
            self.audio_io.stop_speaking()
            self.currently_speaking_event.clear()
        else:
            self.tts_muted_event.clear()
        if self.observability_bus:
            state = "muted" if muted else "unmuted"
            self.observability_bus.emit(
                source="tts",
                kind="mute",
                message=f"TTS {state}",
                meta={"muted": muted},
            )

    def toggle_tts_muted(self) -> bool:
        muted = not self.tts_muted_event.is_set()
        self.set_tts_muted(muted)
        return muted

    def command_specs(self) -> list[CommandSpec]:
        return [self._command_registry[name] for name in self._command_order]

    def submit_text_input(self, text: str, source: str = "text") -> bool:
        text = text.strip()
        if not text:
            return False
        if self.observability_bus:
            self.observability_bus.emit(
                source=source,
                kind="user_input",
                message=trim_message(text),
            )
        # Race-condition fix: mark the user message BEFORE the queue
        # push so context_builder callbacks that read
        # ``interaction_state.last_user_message`` (weather / canon /
        # turn-guard gates) see this turn's utterance, not the
        # previous one. Pre-fix the queue push at line below happened
        # first, LLMProcessor could wake up before mark_user ran, and
        # the gate callbacks then fired on stale or empty content —
        # causing weather_cache to never inject for the current turn.
        if self.interaction_state:
            self.interaction_state.mark_user(message=text)
        self.llm_queue_priority.put(
            {
                "role": "user",
                "content": text,
                "_enqueued_at": time.time(),
                "_lane": "priority",
            }
        )
        # Push user message to emotion agent with repetition-aware severity
        from ..autonomy.agents.emotion_agent import EmotionAgent as _EA
        if self._emotion_agent is not None:
            desc = self._emotion_agent.build_event_description(text)
        else:
            desc = f"User said: {text[:120]}"
        self._push_emotion_event("user", desc)
        self.processing_active_event.set()
        return True

    def autonomy_inflight(self) -> int:
        return self._autonomy_inflight.value()

    def handle_command(self, command: str) -> str:
        text = command.strip()
        if not text:
            return "No command entered."
        if text.startswith("/"):
            text = text[1:]
        parts = text.split()
        if not parts:
            return "No command entered."
        cmd = parts[0].lower()
        args = parts[1:]
        spec = self._command_registry.get(cmd)
        if not spec:
            return f"Unknown command: /{cmd}. Try /help."
        return spec.handler(args)

    def _build_command_registry(self) -> tuple[dict[str, CommandSpec], list[str]]:
        registry: dict[str, CommandSpec] = {}
        order: list[str] = []

        def register(spec: CommandSpec) -> None:
            registry[spec.name] = spec
            order.append(spec.name)
            for alias in spec.aliases:
                registry[alias] = spec

        register(
            CommandSpec(
                name="help",
                description="Show available commands",
                usage="/help",
                handler=self._cmd_help,
                aliases=("?",),
            )
        )
        register(
            CommandSpec(
                name="status",
                description="Show engine status",
                usage="/status",
                handler=self._cmd_status,
            )
        )
        register(
            CommandSpec(
                name="tts",
                description="Control TTS output",
                usage="/tts on|off",
                handler=self._cmd_tts,
            )
        )
        register(
            CommandSpec(
                name="mute-tts",
                description="Mute TTS output",
                usage="/mute-tts",
                handler=self._cmd_mute_tts,
                aliases=("tts-mute",),
            )
        )
        register(
            CommandSpec(
                name="unmute-tts",
                description="Unmute TTS output",
                usage="/unmute-tts",
                handler=self._cmd_unmute_tts,
                aliases=("tts-unmute",),
            )
        )
        register(
            CommandSpec(
                name="quit",
                description="Quit GLaDOS",
                usage="/quit",
                handler=self._cmd_quit,
                aliases=("exit",),
            )
        )
        register(
            CommandSpec(
                name="asr",
                description="Control ASR input",
                usage="/asr on|off",
                handler=self._cmd_asr,
            )
        )
        register(
            CommandSpec(
                name="mute-asr",
                description="Mute ASR input",
                usage="/mute-asr",
                handler=self._cmd_mute_asr,
                aliases=("asr-mute",),
            )
        )
        register(
            CommandSpec(
                name="unmute-asr",
                description="Unmute ASR input",
                usage="/unmute-asr",
                handler=self._cmd_unmute_asr,
                aliases=("asr-unmute",),
            )
        )
        register(
            CommandSpec(
                name="observe",
                description="Open observability screen (TUI)",
                usage="/observe",
                handler=self._cmd_observe,
                aliases=("observability",),
            )
        )
        register(
            CommandSpec(
                name="mcp",
                description="Show MCP server status",
                usage="/mcp status",
                handler=self._cmd_mcp,
            )
        )
        register(
            CommandSpec(
                name="autonomy",
                description="Manage autonomy settings",
                usage="/autonomy on|off | /autonomy debounce on|off",
                handler=self._cmd_autonomy,
            )
        )
        register(
            CommandSpec(
                name="slots",
                description="Show autonomy slots",
                usage="/slots",
                handler=self._cmd_slots,
            )
        )
        register(
            CommandSpec(
                name="minds",
                description="Show active minds",
                usage="/minds",
                handler=self._cmd_minds,
            )
        )
        register(
            CommandSpec(
                name="agents",
                description="Show registered subagents",
                usage="/agents",
                handler=self._cmd_agents,
            )
        )
        register(
            CommandSpec(
                name="emotion",
                description="Show current emotional state",
                usage="/emotion",
                handler=self._cmd_emotion,
            )
        )
        register(
            CommandSpec(
                name="preferences",
                description="Show user preferences",
                usage="/preferences",
                handler=self._cmd_preferences,
            )
        )
        register(
            CommandSpec(
                name="context",
                description="Show context/token usage",
                usage="/context",
                handler=self._cmd_context,
            )
        )
        register(
            CommandSpec(
                name="constitution",
                description="Show constitutional state and modifiers",
                usage="/constitution",
                handler=self._cmd_constitution,
            )
        )
        register(
            CommandSpec(
                name="vision",
                description="Show latest vision snapshot",
                usage="/vision",
                handler=self._cmd_vision,
            )
        )
        register(
            CommandSpec(
                name="config",
                description="Show config summary",
                usage="/config",
                handler=self._cmd_config,
            )
        )
        register(
            CommandSpec(
                name="knowledge",
                description="Manage local knowledge notes",
                usage="/knowledge add|list|set|delete|clear",
                handler=self._cmd_knowledge,
            )
        )
        register(
            CommandSpec(
                name="memory",
                description="Show long-term memory stats",
                usage="/memory",
                handler=self._cmd_memory,
            )
        )
        return registry, order

    def _cmd_help(self, _args: list[str]) -> str:
        lines = ["Commands:"]
        for name in self._command_order:
            spec = self._command_registry[name]
            usage = spec.usage or f"/{spec.name}"
            lines.append(f"- {usage}: {spec.description}")
        return "\n".join(lines)

    def _cmd_status(self, _args: list[str]) -> str:
        autonomy_enabled = self.autonomy_config.enabled
        vision_enabled = self.vision_config is not None
        jobs_enabled = bool(self.autonomy_config.jobs.enabled) if self.autonomy_config else False
        return (
            f"input_mode={self.input_mode}, "
            f"asr_muted={self.asr_muted_event.is_set()}, "
            f"tts_muted={self.tts_muted_event.is_set()}, "
            f"autonomy_enabled={autonomy_enabled}, "
            f"vision_enabled={vision_enabled}, "
            f"jobs_enabled={jobs_enabled}"
        )

    def _cmd_quit(self, _args: list[str]) -> str:
        self.shutdown_event.set()
        return "Shutting down."

    def _cmd_asr(self, args: list[str]) -> str:
        if not args:
            return f"ASR is {'muted' if self.asr_muted_event.is_set() else 'active'}."
        arg = args[0].lower()
        if arg in {"on", "unmute", "active"}:
            self.set_asr_muted(False)
            return "ASR unmuted."
        if arg in {"off", "mute"}:
            self.set_asr_muted(True)
            return "ASR muted."
        return "Usage: /asr on|off"

    def _cmd_tts(self, args: list[str]) -> str:
        if not args:
            return f"TTS is {'muted' if self.tts_muted_event.is_set() else 'active'}."
        arg = args[0].lower()
        if arg in {"on", "unmute", "active"}:
            self.set_tts_muted(False)
            return "TTS unmuted."
        if arg in {"off", "mute"}:
            self.set_tts_muted(True)
            return "TTS muted."
        return "Usage: /tts on|off"

    def _cmd_mute_asr(self, _args: list[str]) -> str:
        self.set_asr_muted(True)
        return "ASR muted."

    def _cmd_unmute_asr(self, _args: list[str]) -> str:
        self.set_asr_muted(False)
        return "ASR unmuted."

    def _cmd_mute_tts(self, _args: list[str]) -> str:
        self.set_tts_muted(True)
        return "TTS muted."

    def _cmd_unmute_tts(self, _args: list[str]) -> str:
        self.set_tts_muted(False)
        return "TTS unmuted."

    def _cmd_observe(self, _args: list[str]) -> str:
        return "Observability is available in the TUI via /observe."

    def _cmd_slots(self, _args: list[str]) -> str:
        if not self.autonomy_slots:
            return "Autonomy slots are unavailable."
        slots = self.autonomy_slots.list_slots()
        if not slots:
            return "No active slots."
        lines = ["Slots:"]
        for slot in slots[:20]:
            summary = slot.summary.strip()
            summary_text = f" - {summary}" if summary else ""
            lines.append(f"- {slot.title}: {slot.status}{summary_text}")
        if len(slots) > 20:
            lines.append(f"... {len(slots) - 20} more")
        return "\n".join(lines)

    def _cmd_minds(self, _args: list[str]) -> str:
        minds = self.mind_registry.snapshot()
        if not minds:
            return "No minds registered."
        lines = ["Minds:"]
        for mind in minds[:20]:
            summary = mind.summary.strip()
            summary_text = f" - {summary}" if summary else ""
            lines.append(f"- {mind.title}: {mind.status}{summary_text}")
        if len(minds) > 20:
            lines.append(f"... {len(minds) - 20} more")
        return "\n".join(lines)

    def _cmd_agents(self, _args: list[str]) -> str:
        if not self.subagent_manager:
            return "Subagent manager is not enabled."
        agents = self.subagent_manager.list_agents()
        if not agents:
            return "No subagents registered."
        lines = ["Subagents:"]
        for agent in agents[:20]:
            status = "running" if agent.running else "stopped"
            tick_info = f"ticks={agent.tick_count}" if agent.tick_count > 0 else "not started"
            lines.append(f"- {agent.title} ({agent.agent_id}): {status}, {tick_info}")
        if len(agents) > 20:
            lines.append(f"... {len(agents) - 20} more")
        return "\n".join(lines)

    def _push_emotion_event(self, source: str, description: str) -> None:
        """Push an event to the emotion agent if it's running."""
        if self._emotion_agent:
            event = EmotionEvent(source=source, description=description)
            self._emotion_agent.push_event(event)

    def _on_tool_event(self, event_type: str, tool_name: str) -> None:
        """Handle tool events for emotional processing."""
        if event_type == "tool_success":
            self._push_emotion_event("system", f"Tool '{tool_name}' completed successfully")
        elif event_type == "tool_failure":
            self._push_emotion_event("system", f"Tool '{tool_name}' failed")
        elif event_type == "tool_timeout":
            self._push_emotion_event("system", f"Tool '{tool_name}' timed out")

    def _cmd_emotion(self, _args: list[str]) -> str:
        if not self._emotion_agent:
            return "Emotion agent is not running."
        state = self._emotion_agent.state
        lines = [
            "Emotional State:",
            f"  Pleasure:  {state.pleasure:+.2f}",
            f"  Arousal:   {state.arousal:+.2f}",
            f"  Dominance: {state.dominance:+.2f}",
            "Mood Baseline:",
            f"  Pleasure:  {state.mood_pleasure:+.2f}",
            f"  Arousal:   {state.mood_arousal:+.2f}",
            f"  Dominance: {state.mood_dominance:+.2f}",
            "",
            state.to_prompt(),
        ]
        return "\n".join(lines)

    def _cmd_preferences(self, _args: list[str]) -> str:
        prefs = self.preferences_store.all()
        if not prefs:
            return "No preferences set."
        lines = ["User Preferences:"]
        for key, value in prefs.items():
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)

    def _cmd_context(self, _args: list[str]) -> str:
        messages = self._conversation_store.snapshot()
        token_count = estimate_tokens(messages)
        msg_count = len(messages)
        system_count = sum(1 for m in messages if m.get("role") == "system")
        user_count = sum(1 for m in messages if m.get("role") == "user")
        assistant_count = sum(1 for m in messages if m.get("role") == "assistant")
        summary_count = sum(
            1 for m in messages
            if isinstance(m.get("content"), str) and m["content"].startswith("[summary]")
        )
        lines = [
            f"Context Usage:",
            f"  Estimated tokens: {token_count}",
            f"  Total messages: {msg_count}",
            f"    System: {system_count}",
            f"    User: {user_count}",
            f"    Assistant: {assistant_count}",
            f"    Summaries: {summary_count}",
        ]
        return "\n".join(lines)

    def _cmd_constitution(self, _args: list[str]) -> str:
        state = self.constitutional_state
        lines = ["Constitutional State:"]
        lines.append("")
        lines.append("Immutable Rules:")
        for rule in state.constitution.immutable_rules:
            lines.append(f"  - {rule}")
        lines.append("")
        lines.append("Modifiable Bounds:")
        for name, (min_val, max_val) in state.constitution.modifiable_bounds.items():
            lines.append(f"  {name}: {min_val} to {max_val}")
        lines.append("")
        if state.active_modifiers:
            lines.append("Active Modifiers:")
            for name, modifier in state.active_modifiers.items():
                lines.append(f"  {name}: {modifier.value} ({modifier.reason})")
        else:
            lines.append("Active Modifiers: none")
        lines.append("")
        lines.append(f"Modifier History: {len(state.modifier_history)} changes")
        return "\n".join(lines)

    def _cmd_vision(self, _args: list[str]) -> str:
        if not self.vision_state:
            return "Vision is disabled."
        snapshot = self.vision_state.snapshot()
        return snapshot or "Vision has no snapshot yet."

    def _cmd_mcp(self, args: list[str]) -> str:
        if not self.mcp_manager:
            return "MCP is disabled."
        if args and args[0].lower() not in {"status", "list"}:
            return "Usage: /mcp status"
        lines = ["MCP servers:"]
        for entry in self.mcp_manager.status_snapshot():
            status = "connected" if entry["connected"] else "offline"
            tools = entry.get("tools", 0)
            resources = entry.get("resources", 0)
            lines.append(f"- {entry['name']}: {status}, tools={tools}, resources={resources}")
        return "\n".join(lines)

    def _cmd_autonomy(self, args: list[str]) -> str:
        if not args:
            return (
                f"Autonomy enabled={self.autonomy_config.enabled}, "
                f"parallel_calls={self.autonomy_config.autonomy_parallel_calls}, "
                f"coalesce_ticks={self.autonomy_config.coalesce_ticks}"
            )
        head = args[0].lower()
        if head in {"on", "off", "true", "false", "enable", "enabled", "disable", "disabled"}:
            enabled = head in {"on", "true", "enable", "enabled"}
            self.autonomy_config.enabled = enabled
            return f"Autonomy {'enabled' if enabled else 'disabled'}."
        if head not in {"coalesce", "debounce"}:
            return "Usage: /autonomy on|off | /autonomy debounce on|off"
        if len(args) == 1:
            return f"Autonomy coalesce_ticks={self.autonomy_config.coalesce_ticks}"
        value = args[1].lower()
        if value in {"on", "true", "enable", "enabled"}:
            self.autonomy_config.coalesce_ticks = True
            return "Autonomy tick coalescing enabled."
        if value in {"off", "false", "disable", "disabled"}:
            self.autonomy_config.coalesce_ticks = False
            return "Autonomy tick coalescing disabled."
        return "Usage: /autonomy on|off | /autonomy debounce on|off"

    def _cmd_config(self, _args: list[str]) -> str:
        jobs_enabled = bool(self.autonomy_config.jobs.enabled) if self.autonomy_config else False
        return (
            f"input_mode={self.input_mode}, "
            f"autonomy.enabled={self.autonomy_config.enabled}, "
            f"autonomy.jobs.enabled={jobs_enabled}, "
            f"autonomy.coalesce_ticks={self.autonomy_config.coalesce_ticks}, "
            f"vision.enabled={self.vision_config is not None}"
        )

    def _cmd_knowledge(self, args: list[str]) -> str:
        if not args or args[0] == "list":
            entries = self.knowledge_store.list_entries()
            if not entries:
                return "Knowledge: no entries."
            lines = ["Knowledge:"]
            for entry in entries[:20]:
                text = entry.text.strip()
                preview = (text[:120] + "...") if len(text) > 120 else text
                lines.append(f"- {entry.entry_id}: {preview}")
            if len(entries) > 20:
                lines.append(f"... {len(entries) - 20} more")
            return "\n".join(lines)

        action = args[0].lower()
        if action == "add":
            text = " ".join(args[1:]).strip()
            if not text:
                return "Usage: /knowledge add <text>"
            entry = self.knowledge_store.add(text)
            return f"Added knowledge #{entry.entry_id}."

        if action in {"set", "update"}:
            if len(args) < 3:
                return "Usage: /knowledge set <id> <text>"
            try:
                entry_id = int(args[1])
            except ValueError:
                return "Knowledge id must be a number."
            text = " ".join(args[2:]).strip()
            if not text:
                return "Usage: /knowledge set <id> <text>"
            updated = self.knowledge_store.update(entry_id, text)
            if not updated:
                return f"Knowledge #{entry_id} not found."
            return f"Updated knowledge #{entry_id}."

        if action in {"delete", "remove"}:
            if len(args) < 2:
                return "Usage: /knowledge delete <id>"
            try:
                entry_id = int(args[1])
            except ValueError:
                return "Knowledge id must be a number."
            removed = self.knowledge_store.delete(entry_id)
            if not removed:
                return f"Knowledge #{entry_id} not found."
            return f"Deleted knowledge #{entry_id}."

        if action == "clear":
            removed = self.knowledge_store.clear()
            return f"Cleared {removed} knowledge entr{'y' if removed == 1 else 'ies'}."

        return "Usage: /knowledge add|list|set|delete|clear"

    def _cmd_memory(self, _args: list[str]) -> str:
        import json
        from pathlib import Path

        memory_dir = Path.home() / ".glados" / "memory"
        facts_file = memory_dir / "facts.jsonl"
        summaries_file = memory_dir / "summaries.jsonl"

        # Count facts
        fact_count = 0
        source_counts: dict[str, int] = {}
        total_importance = 0.0
        if facts_file.exists():
            try:
                with facts_file.open("r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            fact_count += 1
                            try:
                                fact = json.loads(line)
                                source = fact.get("source", "unknown")
                                source_counts[source] = source_counts.get(source, 0) + 1
                                total_importance += fact.get("importance", 0.5)
                            except json.JSONDecodeError:
                                pass
            except OSError:
                pass

        # Count summaries
        summary_count = 0
        period_counts: dict[str, int] = {}
        if summaries_file.exists():
            try:
                with summaries_file.open("r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            summary_count += 1
                            try:
                                summary = json.loads(line)
                                period = summary.get("period", "unknown")
                                period_counts[period] = period_counts.get(period, 0) + 1
                            except json.JSONDecodeError:
                                pass
            except OSError:
                pass

        if fact_count == 0 and summary_count == 0:
            return "Long-term Memory: empty (no facts or summaries stored)"

        avg_importance = total_importance / fact_count if fact_count > 0 else 0.0

        lines = [
            "Long-term Memory Stats:",
            f"  Total facts: {fact_count}",
            f"  Total summaries: {summary_count}",
        ]

        if source_counts:
            lines.append("  Facts by source:")
            for source, count in sorted(source_counts.items()):
                lines.append(f"    {source}: {count}")

        if period_counts:
            lines.append("  Summaries by period:")
            for period, count in sorted(period_counts.items()):
                lines.append(f"    {period}: {count}")

        lines.append(f"  Average importance: {avg_importance:.2f}")
        lines.append(f"  Storage: {memory_dir}")

        return "\n".join(lines)

    def _format_knowledge(self) -> str | None:
        """Format knowledge entries for LLM context."""
        entries = self.knowledge_store.list_entries()
        if not entries:
            return None
        lines = ["[knowledge]"]
        for entry in entries:
            lines.append(f"- #{entry.entry_id}: {entry.text}")
        return "\n".join(lines)

    def _format_slots(self) -> str | None:
        """Format task slots for LLM context."""
        if not self.autonomy_slots:
            return None
        slots = self.autonomy_slots.list_slots()
        if not slots:
            return None
        lines = ["[tasks]"]
        for slot in slots:
            summary = slot.summary.strip()
            summary_text = f" - {summary}" if summary else ""
            lines.append(f"- {slot.title}: {slot.status}{summary_text}")
        return "\n".join(lines)

    def _run_autonomy_ticker(self) -> None:
        assert self.autonomy_event_bus is not None
        logger.info("AutonomyTicker thread started.")
        while not self.shutdown_event.is_set():
            self.autonomy_event_bus.publish(TimeTickEvent(ticked_at=time.time()))
            self.shutdown_event.wait(timeout=self.autonomy_config.tick_interval_s)
        logger.info("AutonomyTicker thread finished.")


if __name__ == "__main__":
    glados_config = GladosConfig.from_yaml("glados_config.yaml")
    glados = Glados.from_config(glados_config)
    glados.run()
