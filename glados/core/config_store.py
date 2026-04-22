"""
Centralized configuration store for GLaDOS.

Loads YAML config files from the configs directory and exposes them through
a validated, thread-safe singleton. Every component imports from here
instead of hardcoding values or loading its own YAML.

Configuration directory is resolved in this order:
  1. GLADOS_CONFIG_DIR environment variable
  2. /app/configs  (container default)
  3. ./configs     (local dev fallback)

All path defaults are driven by environment variables with container-safe
defaults. No Windows paths anywhere in this file.

Usage::

    from glados.core.config_store import cfg

    ha_url   = cfg.ha_url
    ha_token = cfg.ha_token
    tts_url  = cfg.service_url("tts")
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import BaseModel, Field, model_validator

from glados.robots.config import RobotsConfig


# ---------------------------------------------------------------------------
# Environment-variable helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str) -> str:
    """Return env var value or default."""
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# Deprecation helpers — Stage 3 Phase 6
# ---------------------------------------------------------------------------
# Fields marked `deprecated=True` are scheduled for removal after operators
# confirm they're unused (see docs/roadmap.md). A one-line loguru WARNING
# fires at load time if a deprecated field appears in the YAML, so the
# operator can clean the file up without waiting for an error.
#
# Defining the warn at the model level (via `model_validator(mode="after")`)
# keeps the logic next to the field definitions and avoids touching the
# loader. Fields set purely from pydantic defaults do NOT warn — only
# explicit YAML values trigger the message, because `model_fields_set`
# contains only the keys present in the input dict.

def _warn_deprecated_yaml(model: BaseModel, deprecated_fields: dict[str, str]) -> None:
    for field, reason in deprecated_fields.items():
        if field in model.model_fields_set:
            logger.warning(
                "Config field '{}.{}' is deprecated and will be removed: {}",
                type(model).__name__, field, reason,
            )


# Container root — /app in Docker, cwd in dev
_GLADOS_ROOT = _env("GLADOS_ROOT", "/app")
_GLADOS_DATA = _env("GLADOS_DATA", f"{_GLADOS_ROOT}/data")
_GLADOS_AUDIO = _env("GLADOS_AUDIO", f"{_GLADOS_ROOT}/audio_files")
_GLADOS_LOGS = _env("GLADOS_LOGS", f"{_GLADOS_ROOT}/logs")
_GLADOS_ASSETS = _env("GLADOS_ASSETS", f"{_GLADOS_ROOT}/assets")


# ---------------------------------------------------------------------------
# Pydantic models — one per YAML file
# ---------------------------------------------------------------------------

class HomeAssistantGlobal(BaseModel):
    url: str = _env("HA_URL", "http://homeassistant.local:8123")
    ws_url: str = _env("HA_WS_URL", "ws://homeassistant.local:8123/api/websocket")
    token: str = _env("HA_TOKEN", "")

    @model_validator(mode="after")
    def _env_overrides_yaml(self) -> "HomeAssistantGlobal":
        """Environment variables always win over committed YAML for HA
        credentials. Rationale: the real token belongs in deploy-time
        secrets (.env / compose env_file), not in `configs/global.yaml`
        which is checked into git alongside other config. Operators who
        put a placeholder in YAML ("eyJhbG...") will have it overridden
        by the real env value without needing to edit the YAML.
        """
        env_token = os.environ.get("HA_TOKEN", "").strip()
        if env_token:
            self.token = env_token
        env_url = os.environ.get("HA_URL", "").strip()
        if env_url:
            self.url = env_url
        env_ws = os.environ.get("HA_WS_URL", "").strip()
        if env_ws:
            self.ws_url = env_ws
        return self


class NetworkGlobal(BaseModel):
    serve_host: str = Field(default=_env("SERVE_HOST", ""), deprecated=True)
    serve_port: int = Field(default=int(_env("SERVE_PORT", "5051")), deprecated=True)

    @model_validator(mode="after")
    def _warn_deprecated(self) -> "NetworkGlobal":
        _warn_deprecated_yaml(self, {
            "serve_host": "env-driven (SERVE_HOST); YAML is ignored inside the container",
            "serve_port": "env-driven (SERVE_PORT); YAML is ignored inside the container",
        })
        return self


class PathsGlobal(BaseModel):
    glados_root: str = Field(default=_GLADOS_ROOT, deprecated=True)
    audio_base: str = Field(default=_GLADOS_AUDIO, deprecated=True)
    logs: str = Field(default=_GLADOS_LOGS, deprecated=True)
    data: str = Field(default=_GLADOS_DATA, deprecated=True)
    assets: str = Field(default=_GLADOS_ASSETS, deprecated=True)

    @model_validator(mode="after")
    def _warn_deprecated(self) -> "PathsGlobal":
        _warn_deprecated_yaml(self, {
            "glados_root": "env-driven (GLADOS_ROOT); YAML is ignored inside the container",
            "audio_base": "env-driven (GLADOS_AUDIO); YAML is ignored inside the container",
            "logs": "env-driven (GLADOS_LOGS); YAML is ignored inside the container",
            "data": "env-driven (GLADOS_DATA); YAML is ignored inside the container",
            "assets": "env-driven (GLADOS_ASSETS); YAML is ignored inside the container",
        })
        return self


class SSLGlobal(BaseModel):
    enabled: bool = _env("SSL_ENABLED", "false").lower() == "true"
    domain: str = _env("ACME_DOMAIN", "")
    cert_path: str = _env("SSL_CERT", f"{_GLADOS_ROOT}/certs/cert.pem")
    key_path: str = _env("SSL_KEY", f"{_GLADOS_ROOT}/certs/key.pem")
    use_letsencrypt: bool = False
    acme_email: str = _env("ACME_EMAIL", "")
    acme_provider: str = _env("DNS_PROVIDER", "cloudflare")
    acme_api_token: str = _env("DNS_API_TOKEN", "")


class AuthGlobal(BaseModel):
    enabled: bool = True
    password_hash: str = ""
    session_secret: str = ""
    session_timeout_hours: int = 24


class AuditGlobal(BaseModel):
    """Stage 3 Phase 0: JSON-lines audit log for utterances and tool calls."""
    enabled: bool = True
    path: str = Field(default=f"{_GLADOS_LOGS}/audit.jsonl", deprecated=True)
    retention_days: int = Field(default=30, deprecated=True)  # Rotation not implemented.

    @model_validator(mode="after")
    def _warn_deprecated(self) -> "AuditGlobal":
        _warn_deprecated_yaml(self, {
            "path": "env-driven (GLADOS_LOGS); YAML is ignored inside the container",
            "retention_days": "audit rotation is not implemented; field has no effect",
        })
        return self


class ModeEntitiesGlobal(BaseModel):
    maintenance_mode: str = "input_boolean.glados_maintenance_mode"
    maintenance_speaker: str = "input_text.glados_maintenance_speaker"
    silent_mode: str = "input_boolean.glados_silent_mode"
    dnd: str = "input_boolean.glados_dnd"


class SilentHoursGlobal(BaseModel):
    enabled: bool = True
    start: str = "22:00"
    end: str = "07:00"
    min_tier: str = "HIGH"


class TuningGlobal(BaseModel):
    llm_connect_timeout_s: int = 10
    llm_read_timeout_s: int = 180
    tts_flush_chars: int = 150
    engine_pause_time: float = 0.05
    mode_cache_ttl_s: float = 5.0
    engine_audio_default: bool = Field(default=True, deprecated=True)

    @model_validator(mode="after")
    def _warn_deprecated(self) -> "TuningGlobal":
        _warn_deprecated_yaml(self, {
            "engine_audio_default": "no code consumers; field has no effect",
        })
        return self


class WeatherGlobal(BaseModel):
    """Consolidated weather configuration (Phase 6.4 — 2026-04-22).

    Before this pass, unit preferences were split between WeatherGlobal
    (marked deprecated, unused) and autonomy's WeatherJobConfig — the
    UI would edit one while the fetcher read the other. Consolidated
    here so the Integrations → Weather tab has a single source of
    truth. The autonomy subagent still holds tuning knobs (poll
    interval, alert thresholds) separately since those are autonomy-
    specific and not operator-facing.

    Provider is Open-Meteo (free, no API key). The geocoding endpoint
    translates postal code / city / address into lat-long; the forecast
    endpoint consumes the resolved coordinates. location_name is the
    friendly string shown in the UI (e.g. 'Fort Worth, Texas, US') so
    operators see *where* they're pointed, not just raw coordinates.
    """

    # Location — lat/lng are authoritative; location_name and the
    # auto-from-HA flag are UI convenience.
    latitude: float = 0.0
    longitude: float = 0.0
    auto_from_ha: bool = True
    location_name: str = ""

    # Unit preferences — passed to Open-Meteo query params so the API
    # returns values in the operator's chosen units directly.
    temperature_unit: str = "fahrenheit"   # 'celsius' | 'fahrenheit'
    wind_speed_unit: str = "mph"           # 'mph' | 'kmh' | 'ms' | 'kn'
    precipitation_unit: str = "inch"       # 'inch' | 'mm'
    timezone: str = "auto"                 # Open-Meteo IANA or 'auto'


class GlobalConfig(BaseModel):
    home_assistant: HomeAssistantGlobal = HomeAssistantGlobal()
    network: NetworkGlobal = NetworkGlobal()
    paths: PathsGlobal = PathsGlobal()
    ssl: SSLGlobal = SSLGlobal()
    auth: AuthGlobal = AuthGlobal()
    audit: AuditGlobal = AuditGlobal()
    mode_entities: ModeEntitiesGlobal = ModeEntitiesGlobal()
    silent_hours: SilentHoursGlobal = SilentHoursGlobal()
    tuning: TuningGlobal = TuningGlobal()
    weather: WeatherGlobal = WeatherGlobal()


class ServiceEndpoint(BaseModel):
    url: str
    voice: str | None = None
    model: str | None = None


class ServicesConfig(BaseModel):
    tts: ServiceEndpoint = ServiceEndpoint(
        url=_env("SPEACHES_URL", "http://speaches:8800"),
        voice=_env("TTS_VOICE", "glados"),
        model=_env("TTS_MODEL", "hexgrad/Kokoro-82M"),
    )
    stt: ServiceEndpoint = ServiceEndpoint(
        url=_env("SPEACHES_URL", "http://speaches:8800"),
        model=_env("STT_MODEL", "Systran/faster-whisper-small"),
    )
    api_wrapper: ServiceEndpoint = ServiceEndpoint(
        url=f"http://localhost:{_env('GLADOS_PORT', '8015')}"
    )
    vision: ServiceEndpoint = ServiceEndpoint(
        url=_env("VISION_URL", "http://glados-vision:8016")
    )
    ollama_interactive: ServiceEndpoint = ServiceEndpoint(
        url=_env("OLLAMA_URL", "http://ollama:11434")
    )
    ollama_autonomy: ServiceEndpoint = ServiceEndpoint(
        url=_env("OLLAMA_AUTONOMY_URL", _env("OLLAMA_URL", "http://ollama:11434"))
    )
    ollama_vision: ServiceEndpoint = ServiceEndpoint(
        url=_env("OLLAMA_VISION_URL", _env("OLLAMA_URL", "http://ollama:11434"))
    )
    gladys_api: ServiceEndpoint = Field(
        default=ServiceEndpoint(url="http://localhost:8020"),
        deprecated=True,
    )

    @model_validator(mode="after")
    def _warn_deprecated(self) -> "ServicesConfig":
        _warn_deprecated_yaml(self, {
            "gladys_api": "reserved endpoint with no code consumers; will be removed",
        })
        return self


class SpeakersConfig(BaseModel):
    default: str = ""
    available: list[str] = []
    blacklist: list[str] = []


class AudioConfig(BaseModel):
    ha_output_dir: str = f"{_GLADOS_AUDIO}/glados_ha"
    archive_dir: str = f"{_GLADOS_AUDIO}/glados_archive"
    archive_max_files: int = 50
    tts_ui_output_dir: str = f"{_GLADOS_AUDIO}/glados_tts_ui"
    tts_ui_max_files: int = 50
    chat_audio_dir: str = f"{_GLADOS_AUDIO}/chat_audio"
    chat_audio_max_files: int = 100
    announcements_dir: str = f"{_GLADOS_AUDIO}/glados_announcements"
    commands_dir: str = f"{_GLADOS_AUDIO}/glados_commands"
    chimes_dir: str = f"{_GLADOS_AUDIO}/chimes"
    # Phase 5.9.2.2: sound-library categories — one folder per category
    # under this path. Parallel to chimes_dir. Accessed by the TTS
    # Save-to-category flow and the HA-trigger dispatcher.
    sounds_dir: str = f"{_GLADOS_AUDIO}/sounds"
    silence_between_sentences_ms: int = 400
    sample_rate: int = 24000

    # Phase 8.11 — streaming-TTS pacing knobs. ``first_tts_flush_chars``
    # sets the char threshold before the FIRST sentence fires to TTS —
    # low enough that short greetings (``"Affirmative."``) don't stall
    # waiting to accumulate more text. ``min_tts_flush_chars`` sets the
    # threshold for every subsequent batch. Both are upper bounds: the
    # sentence-boundary detector in ``LLMProcessor`` flushes on the
    # first ``.?!`` regardless of count, so a complete short sentence
    # never waits. The legacy ``streaming_tts_chunk_chars`` field on
    # the ``Glados`` YAML block is kept for back-compat; the engine
    # reconciliation prefers these AudioConfig values when present.
    first_tts_flush_chars: int = 30
    min_tts_flush_chars: int = 80
    # Enable flushing whenever the accumulated text ends in a sentence-
    # terminator (``. ! ? ?!``). Disable for an A/B with the pre-8.11
    # char-threshold-only behaviour. Rarely needed, but operators
    # debugging choppy TTS can toggle it from the Audio page.
    sentence_boundary_flush: bool = True


class TTSParams(BaseModel):
    length_scale: float = 1.0
    noise_scale: float = 0.667
    noise_w: float = 0.8


class AttitudeEntry(BaseModel):
    tag: str
    label: str
    directive: str
    tts: TTSParams = TTSParams()
    weight: float = 1.0


class HEXACOPersonality(BaseModel):
    honesty_humility: float = 0.3
    emotionality: float = 0.7
    extraversion: float = 0.4
    agreeableness: float = 0.2
    conscientiousness: float = 0.9
    openness: float = 0.95


class EmotionPersonality(BaseModel):
    enabled: bool = True
    tick_interval_s: float = 30.0
    max_events: int = 20
    baseline_pleasure: float = 0.1
    baseline_arousal: float = -0.1
    baseline_dominance: float = 0.6
    mood_drift_rate: float = 0.1
    baseline_drift_rate: float = 0.02


class PrepromptEntry(BaseModel):
    system: str | None = None
    user: str | None = None
    assistant: str | None = None


class ModelOptionsConfig(BaseModel):
    """Ollama-style model parameters sent in the request `options` dict.

    Stage 3 Phase A: lifted out of hardcoded values in api_wrapper so the
    operator can tune persona strength without code changes. Critical
    when running a neutral base model (e.g. qwen2.5:14b-instruct) instead
    of a Modelfile-tuned `glados:latest` — temperature/top_p directly
    affect how strongly the container's personality_preprompt steers the
    model's voice.

    Env-overrides-YAML pattern: `OLLAMA_TEMPERATURE`, `OLLAMA_TOP_P`,
    `OLLAMA_NUM_CTX`, `OLLAMA_REPEAT_PENALTY` win when set. Operator can
    leave the YAML as a sensible default and override per-deployment.
    """
    temperature: float = 0.7
    top_p: float = 0.9
    num_ctx: int = 16384
    repeat_penalty: float = 1.1

    @model_validator(mode="after")
    def _env_overrides_yaml(self) -> "ModelOptionsConfig":
        for env_key, attr, cast in [
            ("OLLAMA_TEMPERATURE", "temperature", float),
            ("OLLAMA_TOP_P", "top_p", float),
            ("OLLAMA_NUM_CTX", "num_ctx", int),
            ("OLLAMA_REPEAT_PENALTY", "repeat_penalty", float),
        ]:
            raw = os.environ.get(env_key, "").strip()
            if not raw:
                continue
            try:
                setattr(self, attr, cast(raw))
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid {}={!r}", env_key, raw)
        return self

    def to_ollama_options(self) -> dict[str, Any]:
        """Build the `options` dict sent in the Ollama POST body."""
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "num_ctx": self.num_ctx,
            "repeat_penalty": self.repeat_penalty,
        }


class PersonalityConfig(BaseModel):
    default_tts: TTSParams = TTSParams()
    hexaco: HEXACOPersonality = HEXACOPersonality()
    emotion: EmotionPersonality = EmotionPersonality()
    attitudes: list[AttitudeEntry] = []
    preprompt: list[PrepromptEntry] = []
    model_options: ModelOptionsConfig = ModelOptionsConfig()


class DiscordConfig(BaseModel):
    bot_token: str = _env("DISCORD_BOT_TOKEN", "")
    active_channels: list[int] = []
    alert_channel: int = 0
    allowed_user_ids: list[int] = []
    max_history_per_channel: int = 50
    max_message_length: int = 2000
    presence_status: str = "Monitoring the Enrichment Center"


class MemoryConfig(BaseModel):
    chromadb_host: str = _env("CHROMADB_HOST", "chromadb")
    chromadb_port: int = int(_env("CHROMADB_PORT", "8000"))
    retrieval_count: int = 5
    episodic_ttl_hours: int = 168
    summarization_threshold_hours: int = 72
    summarization_cron: str = "0 3 * * *"

    # Stage 3 Phase C: conversation retention.
    # Raw turn-by-turn history lives in the SQLite ConversationDB at
    # /app/data/conversation.db. Compaction summaries persist longer
    # in ChromaDB (semantic collection) so long-term context survives
    # even after the raw transcript is pruned.
    #
    # Hard cap: 180 days regardless of operator setting. Six months is
    # the maximum "we know nothing about you" forgetting window for a
    # household device. WebUI editor warns + clamps if the operator
    # tries to set max_days higher than this.
    conversation_max_days: int = 30
    conversation_hard_cap_days: int = 180
    conversation_max_disk_mb: int = 500
    chromadb_max_disk_mb: int = 2000

    # How often the retention sweeper runs (seconds).
    retention_sweep_interval_s: int = 3600  # hourly

    # Phase 8.8 — Follow-up window for anaphoric carry-over.
    # Short-term SessionMemory TTL (seconds). Utterances like
    # "turn it up more" or "do that again" within this window of
    # the most-recent Tier 1/2 action inherit that action's
    # entity/service as the implied target. 600 s (10 min) is the
    # default; operators who walk away and come back mid-command
    # can shorten, those with slower dialog cadence can lengthen.
    # Read by SessionMemory at engine boot; value takes effect on
    # the next hot-reload of the engine.
    session_idle_ttl_seconds: int = 600

    # Stage 3 Phase 5: passive memory dedup-with-reinforcement.
    # Passive-extracted facts default to review_status="approved" so
    # they enter RAG immediately (the old "pending" flow required
    # operator promotion, which made the feature effectively unused).
    # On each subsequent similar mention, the existing fact is
    # reinforced (importance bumped, mention_count incremented) via
    # cosine-distance matching in ChromaDB rather than duplicating.
    # Operator can set passive_default_status="pending" to restore the
    # review-queue flow; the Memory UI renders a Pending panel in that
    # case. See docs/CHANGES.md Change 10.
    passive_default_status: str = "approved"
    passive_dedup_threshold: float = 0.30
    passive_base_importance: float = 0.5
    passive_reinforce_step: float = 0.05
    passive_importance_cap: float = 0.95


class TtsPronunciationConfig(BaseModel):
    """Phase 8.10 — deterministic pre-TTS text normalization.

    Piper (via Speaches) mispronounces common short abbreviations.
    Operator-flagged cases: ``AI`` is read as "Aye" (one letter)
    because ``SpokenTextConverter``'s all-caps splitter turns it into
    ``"A I"`` which Piper then slurs; ``HA`` as "H A" reads
    mechanically. A pre-pass *before* the all-caps split gives each
    abbreviation a direct spoken expansion the TTS pronounces cleanly.

    Two maps, evaluated in this order inside
    ``SpokenTextConverter.text_to_spoken``:

    1. ``symbol_expansions`` — literal str.replace on non-alphabetic
       keys (``"%"`` → ``" percent"``, ``"&"`` → ``" and "``).
       Runs first so ``"10%"`` becomes ``"10 percent"`` before any
       word-boundary logic.
    2. ``word_expansions`` — case-insensitive whole-word match on
       alphabetic keys (``"AI"`` → ``"Aye Eye"``, ``"TV"`` →
       ``"Tee Vee"``). Runs before the all-caps splitter so the
       acronym never gets reduced to single letters.

    Keys in either map may be edited / removed / added via the Audio
    & Speakers WebUI card. Defaults cover the operator-reported
    cases from the 2026-04-20 pronunciation audit. Adding more (for
    instance, "SSL" → "S S L" if Piper stumbles on that) is a text
    edit, not a deploy.
    """

    symbol_expansions: dict[str, str] = {
        "%": " percent",
        "&": " and ",
        "@": " at ",
    }

    word_expansions: dict[str, str] = {
        "AI": "Aye Eye",
        "HA": "Home Assistant",
        "TV": "Tee Vee",
        "IoT": "I o T",
    }


class TestHarnessConfig(BaseModel):
    """Phase 8.9 — external test-battery scoring knobs.

    The GLaDOS container does not run the battery itself; the harness
    lives in a separate scratch dir. But the harness needs two things
    from the operator-editable config surface here so that its scoring
    matches the production install's reality:

    1. ``noise_entity_patterns`` — fnmatch-style globs on ``entity_id``
       for entities whose state flips randomly in the background
       (Midea AC displays cycling every 60 s, Sonos diagnostics, WLED
       "reverse" toggles, zigbee ``*_button_indication`` /
       ``*_node_identify`` housekeeping entities). If any of those
       entities flip during a test window the diff scorer was counting
       the test as a PASS even when nothing GLaDOS-commanded actually
       moved. Harness filters ``changed_entities`` against these globs
       before scoring.
    2. ``require_direction_match`` — when True the harness demands the
       targeted entity (matched by ``target_keywords`` on the test row)
       finished in the expected state ('on', 'off', or a brightness /
       colour delta matching the verb's direction). When False the
       pre-8.9 "any change counts" scoring is preserved, for
       back-compat A/B.

    Public read-only retrieval at ``GET /api/test-harness/noise-patterns``
    — the external harness pulls this before every run so operators
    don't keep two copies of the noise list in sync.
    """

    # Opt out of pytest collection — the class name starts with "Test"
    # which pytest treats as a test class absent this hint.
    __test__ = False

    # fnmatch globs — leading/trailing ``*`` wildcards OK.
    noise_entity_patterns: list[str] = [
        "switch.midea_ac_*_display",
        "sensor.midea_ac_*_*",
        "*_sonos_*",
        "*_wled_*_reverse",
        "*_button_indication",
        "*_node_identify",
    ]

    require_direction_match: bool = True


class ObserverEntityRule(BaseModel):
    entity_id: str
    category: str = "notable"
    alert: bool = False


class ObserverConfig(BaseModel):
    enabled: bool = True
    entity_whitelist: list[ObserverEntityRule] = []
    # Empty default → consumers resolve via cfg.service_model("ollama_autonomy").
    # "Nothing hardcoded" principle: operator's LLM & Services page selection
    # is the single source of truth for every LLM consumer.
    judgment_model: str = ""
    judgment_ollama_url: str = ""
    alert_cooldown_s: int = 300
    nightly_summary_hour: int = 3


class Hub75GazeConfig(BaseModel):
    enabled: bool = True
    range_x: float = 10.0
    range_y: float = 8.0
    saccade_speed: float = 40.0
    fixation_min: float = 1.0
    fixation_max: float = 4.0
    blink_interval_min: float = 3.0
    blink_interval_max: float = 7.0
    blink_duration: float = 0.15


class Hub75InfoEntityConfig(BaseModel):
    entity_id: str
    label: str = ""
    ok_state: str = "off"


class Hub75InfoPanelConfig(BaseModel):
    enabled: bool = False
    brightness: float = 0.4
    weather_interval_s: float = 60.0
    home_interval_s: float = 45.0
    home_entities: list[Hub75InfoEntityConfig] = []


class Hub75DisplayConfig(BaseModel):
    enabled: bool = False
    wled_ip: str = _env("HUB75_WLED_IP", "")
    wled_ddp_port: int = 4048
    panel_width: int = 64
    panel_height: int = 64
    global_brightness: int = 200
    fps: int = 15
    ddp_inter_packet_delay_ms: float = 1.5
    transition_duration: float = 0.4
    idle_timeout: int = 300
    assets_dir: str = f"{_GLADOS_ASSETS}/display"
    presets: dict[str, int] = {}
    eye_state_overrides: dict[str, dict[str, float]] = {}
    gaze: Hub75GazeConfig = Hub75GazeConfig()
    info_panel: Hub75InfoPanelConfig = Hub75InfoPanelConfig()


class SoundFileEntry(BaseModel):
    """One audio file inside a sound category folder.

    Tracked in sound_categories.yaml so operators can enable/disable
    individual files (keep the recording around but bar it from the
    random pool) and annotate them with notes. The file itself lives
    on disk under configs/sounds/<category>/<name>.
    """
    enabled: bool = True
    added: str = ""        # ISO date string
    note: str = ""         # operator annotation, optional


class SoundCategory(BaseModel):
    """One named category of GLaDOS-voiced audio.

    Categories are the unit HA pulls on: for each enabled category,
    GLaDOS registers an input_button helper via HA's REST API on
    startup, and dispatches the configured action_kind when the
    corresponding state_changed event arrives over the WebSocket.
    """
    name: str
    description: str = ""
    action_kind: str = "audio_random"
    # action_kind values:
    #   audio_random                  - pick any file (enabled flags ignored; UI hides them)
    #   audio_specific                - play selected_file every time
    #   audio_random_from_enabled_files - pick randomly from files with enabled=true
    #   llm                           - compose live via llm_preset; no files consumed

    llm_preset: str = ""
    # One of: morning_greeting, arrival_greeting, departure_farewell,
    # weather_alert, appliance_done, generic_announce. Only used when
    # action_kind == 'llm'.

    selected_file: str = ""
    # Only used when action_kind == 'audio_specific'. Filename within
    # the category folder (no path).

    ha_exposed: bool = True
    # When true, GLaDOS publishes input_button.glados_<name> to HA on
    # startup. Set false to keep the category operational but
    # unexposed (e.g., while still recording the first files).

    speaker: str | None = None
    # HA media_player entity_id to route audio through, or None to
    # use the Speakers-picker default.

    files: dict[str, SoundFileEntry] = {}
    # Per-file metadata. Keys are filenames (no path). Populated by
    # the TTS save-to-category flow; operator edits enable flags.


class SoundCategoriesConfig(BaseModel):
    """Registry of named audio categories.

    Each category is a folder under configs/sounds/<name>/ plus the
    metadata here. When the TTS page saves a new recording to a
    category, it writes the audio file AND upserts a SoundFileEntry
    into files[] for that category. The operator then enables /
    disables individual files via the Sound Categories UI card.
    """
    version: int = 1
    categories: list[SoundCategory] = []


class MQTTConfig(BaseModel):
    """MQTT peer bus configuration.

    Everything here is operator-editable via the Integrations → MQTT
    card in the WebUI — no hardcoded broker host, port, topic prefix,
    or credentials anywhere in the code. Same pattern as the HA
    connection: config is the only source of truth.

    When `enabled` is false (default), the client never connects and
    no events are published or subscribed. When `auth_enabled` is
    false, username/password are ignored and the broker is connected
    anonymously (useful for local dev brokers without auth).

    Typical HA Mosquitto setup:
      broker_host = 10.0.0.20
      broker_port = 1883
      auth_enabled = true
      username = glados-bridge
      password = <from HA user list>
    """

    enabled: bool = False

    # Broker connection.
    broker_host: str = ""
    broker_port: int = 1883
    use_tls: bool = False

    # Auth is per-broker. HA's Mosquitto add-on almost always requires
    # authentication; local dev brokers often don't.
    auth_enabled: bool = False
    username: str = ""
    password: str = ""

    # Identity + topic routing. topic_prefix is the root for both
    # outbound events ({prefix}/events/...) and inbound commands
    # ({prefix}/cmd/...). Keep it short and operator-meaningful.
    client_id: str = "glados-bridge"
    topic_prefix: str = "glados"

    # Transport tuning — leave alone unless you know why.
    keepalive_s: int = 60
    reconnect_delay_s: int = 5


# ---------------------------------------------------------------------------
# Unified config store
# ---------------------------------------------------------------------------

class GladosConfigStore:
    """Thread-safe, lazily-loaded configuration singleton.

    Config directory resolution order:
      1. GLADOS_CONFIG_DIR environment variable
      2. /app/configs  (container default)
      3. ./configs     (local dev fallback)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded = False
        self._configs_dir: Path = self._resolve_config_dir()

        self._global: GlobalConfig = GlobalConfig()
        self._services: ServicesConfig = ServicesConfig()
        self._speakers: SpeakersConfig = SpeakersConfig()
        self._audio: AudioConfig = AudioConfig()
        self._personality: PersonalityConfig = PersonalityConfig()
        self._discord: DiscordConfig = DiscordConfig()
        self._memory: MemoryConfig = MemoryConfig()
        self._observer: ObserverConfig = ObserverConfig()
        self._hub75: Hub75DisplayConfig = Hub75DisplayConfig()
        self._robots: RobotsConfig = RobotsConfig()
        self._test_harness: TestHarnessConfig = TestHarnessConfig()
        self._tts_pronunciation: TtsPronunciationConfig = TtsPronunciationConfig()
        self._mqtt: MQTTConfig = MQTTConfig()
        self._sound_categories: SoundCategoriesConfig = SoundCategoriesConfig()

    @staticmethod
    def _resolve_config_dir() -> Path:
        if env_dir := os.environ.get("GLADOS_CONFIG_DIR"):
            return Path(env_dir)
        container_default = Path("/app/configs")
        if container_default.exists():
            return container_default
        return Path("configs")

    # ── Loading ────────────────────────────────────────────────

    def load(self, configs_dir: str | Path | None = None) -> None:
        with self._lock:
            if configs_dir is not None:
                self._configs_dir = Path(configs_dir)
            self._load_all()
            self._loaded = True

    def reload(self) -> None:
        self.load()

    def _load_all(self) -> None:
        d = self._configs_dir
        self._global = self._load_model(d / "global.yaml", GlobalConfig)
        self._services = self._load_model(d / "services.yaml", ServicesConfig)
        self._speakers = self._load_model(d / "speakers.yaml", SpeakersConfig)
        self._audio = self._load_model(d / "audio.yaml", AudioConfig)
        self._personality = self._load_model(d / "personality.yaml", PersonalityConfig)
        self._discord = self._load_model(d / "discord.yaml", DiscordConfig)
        self._memory = self._load_model(d / "memory.yaml", MemoryConfig)
        self._observer = self._load_model(d / "observer.yaml", ObserverConfig)
        self._hub75 = self._load_model(d / "hub75.yaml", Hub75DisplayConfig)
        self._robots = self._load_model(d / "robots.yaml", RobotsConfig)
        self._test_harness = self._load_model(
            d / "test_harness.yaml", TestHarnessConfig,
        )
        self._tts_pronunciation = self._load_model(
            d / "tts_pronunciation.yaml", TtsPronunciationConfig,
        )
        self._mqtt = self._load_model(d / "mqtt.yaml", MQTTConfig)
        self._sound_categories = self._load_model(
            d / "sound_categories.yaml", SoundCategoriesConfig,
        )
        logger.info("Config store loaded from {}", d)

    @staticmethod
    def _load_model(path: Path, model_cls: type[BaseModel]) -> BaseModel:
        if not path.exists():
            logger.debug("Config not found, using defaults: {}", path)
            return model_cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return model_cls.model_validate(raw)

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ── Accessors ──────────────────────────────────────────────

    @property
    def configs_dir(self) -> Path:
        """Resolved config directory — parent of all YAML files and the
        sound-library root (configs/sounds/<category>/)."""
        self._ensure_loaded()
        return self._configs_dir

    @property
    def sound_categories(self) -> SoundCategoriesConfig:
        self._ensure_loaded()
        return self._sound_categories

    @property
    def mqtt(self) -> MQTTConfig:
        self._ensure_loaded()
        return self._mqtt

    @property
    def ha_url(self) -> str:
        self._ensure_loaded()
        return self._global.home_assistant.url.rstrip("/")

    @property
    def ha_token(self) -> str:
        self._ensure_loaded()
        return self._global.home_assistant.token

    @property
    def ha_ws_url(self) -> str:
        self._ensure_loaded()
        return self._global.home_assistant.ws_url

    @property
    def serve_host(self) -> str:
        self._ensure_loaded()
        return self._global.network.serve_host

    @property
    def serve_port(self) -> int:
        self._ensure_loaded()
        return self._global.network.serve_port

    @property
    def glados_root(self) -> str:
        self._ensure_loaded()
        return self._global.paths.glados_root

    @property
    def audio_base(self) -> str:
        self._ensure_loaded()
        return self._global.paths.audio_base

    @property
    def ssl(self) -> SSLGlobal:
        self._ensure_loaded()
        return self._global.ssl

    @property
    def auth(self) -> AuthGlobal:
        self._ensure_loaded()
        return self._global.auth

    @property
    def audit(self) -> AuditGlobal:
        self._ensure_loaded()
        return self._global.audit

    @property
    def mode_entities(self) -> ModeEntitiesGlobal:
        self._ensure_loaded()
        return self._global.mode_entities

    @property
    def silent_hours(self) -> SilentHoursGlobal:
        self._ensure_loaded()
        return self._global.silent_hours

    @property
    def tuning(self) -> TuningGlobal:
        self._ensure_loaded()
        return self._global.tuning

    @property
    def weather(self) -> WeatherGlobal:
        self._ensure_loaded()
        return self._global.weather

    def service_url(self, name: str) -> str:
        self._ensure_loaded()
        ep: ServiceEndpoint | None = getattr(self._services, name, None)
        if ep is None:
            raise KeyError(f"Unknown service: {name!r}")
        return ep.url.rstrip("/")

    def service_model(self, name: str, *, fallback: str | None = None) -> str:
        """Return the operator-selected model for the named service
        endpoint (LLM & Services WebUI page). Falls back to the
        `fallback` argument (typically another service's model) then to
        an empty string.

        Single source of truth for "what model should this LLM-consuming
        code path call?" — lets hot-reloads propagate model swaps to
        every consumer (disambiguator, rewriter, chat, autonomy, judgment,
        doorbell screener, etc.) without any hard-coded defaults.
        """
        self._ensure_loaded()
        ep: ServiceEndpoint | None = getattr(self._services, name, None)
        if ep is not None:
            model = (ep.model or "").strip()
            if model:
                return model
        if fallback:
            fb = fallback.strip()
            if fb:
                return fb
        return ""

    @property
    def services(self) -> ServicesConfig:
        self._ensure_loaded()
        return self._services

    @property
    def speakers(self) -> SpeakersConfig:
        self._ensure_loaded()
        return self._speakers

    @property
    def audio(self) -> AudioConfig:
        self._ensure_loaded()
        return self._audio

    @property
    def personality(self) -> PersonalityConfig:
        self._ensure_loaded()
        return self._personality

    @property
    def discord(self) -> DiscordConfig:
        self._ensure_loaded()
        return self._discord

    @property
    def memory(self) -> MemoryConfig:
        self._ensure_loaded()
        return self._memory

    @property
    def observer(self) -> ObserverConfig:
        self._ensure_loaded()
        return self._observer

    @property
    def hub75(self) -> Hub75DisplayConfig:
        self._ensure_loaded()
        return self._hub75

    @property
    def robots(self) -> RobotsConfig:
        self._ensure_loaded()
        return self._robots

    @property
    def test_harness(self) -> TestHarnessConfig:
        self._ensure_loaded()
        return self._test_harness

    @property
    def tts_pronunciation(self) -> TtsPronunciationConfig:
        self._ensure_loaded()
        return self._tts_pronunciation

    @property
    def global_(self) -> GlobalConfig:
        self._ensure_loaded()
        return self._global

    # ── Serialisation helpers ──────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        self._ensure_loaded()
        return {
            "global": self._global.model_dump(),
            "services": self._services.model_dump(exclude_none=True),
            "speakers": self._speakers.model_dump(),
            "audio": self._audio.model_dump(),
            "personality": self._personality.model_dump(),
            "discord": self._discord.model_dump(),
            "memory": self._memory.model_dump(),
            "observer": self._observer.model_dump(),
            "hub75": self._hub75.model_dump(),
            "robots": self._robots.model_dump(),
            "test_harness": self._test_harness.model_dump(),
            "tts_pronunciation": self._tts_pronunciation.model_dump(),
            "mqtt": self._mqtt.model_dump(),
            "sound_categories": self._sound_categories.model_dump(),
        }

    def update_section(self, section: str, data: dict) -> None:
        model_map: dict[str, tuple[type[BaseModel], str]] = {
            "global": (GlobalConfig, "global.yaml"),
            "services": (ServicesConfig, "services.yaml"),
            "speakers": (SpeakersConfig, "speakers.yaml"),
            "audio": (AudioConfig, "audio.yaml"),
            "personality": (PersonalityConfig, "personality.yaml"),
            "discord": (DiscordConfig, "discord.yaml"),
            "memory": (MemoryConfig, "memory.yaml"),
            "observer": (ObserverConfig, "observer.yaml"),
            "hub75": (Hub75DisplayConfig, "hub75.yaml"),
            "robots": (RobotsConfig, "robots.yaml"),
            "test_harness": (TestHarnessConfig, "test_harness.yaml"),
            "tts_pronunciation": (TtsPronunciationConfig, "tts_pronunciation.yaml"),
            "mqtt": (MQTTConfig, "mqtt.yaml"),
            "sound_categories": (SoundCategoriesConfig, "sound_categories.yaml"),
        }
        if section not in model_map:
            raise KeyError(f"Unknown config section: {section!r}")

        model_cls, filename = model_map[section]
        validated = model_cls.model_validate(data)
        path = self._configs_dir / filename
        yaml_str = yaml.dump(
            validated.model_dump(), default_flow_style=False, sort_keys=False,
        )
        path.write_text(yaml_str, encoding="utf-8")
        self.reload()


# Module-level singleton
cfg = GladosConfigStore()
