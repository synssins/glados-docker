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
from pydantic import BaseModel

from glados.robots.config import RobotsConfig


# ---------------------------------------------------------------------------
# Environment-variable helpers
# ---------------------------------------------------------------------------

def _env(key: str, default: str) -> str:
    """Return env var value or default."""
    return os.environ.get(key, default)


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


class NetworkGlobal(BaseModel):
    serve_host: str = _env("SERVE_HOST", "")
    serve_port: int = int(_env("SERVE_PORT", "5051"))


class PathsGlobal(BaseModel):
    glados_root: str = _GLADOS_ROOT
    audio_base: str = _GLADOS_AUDIO
    logs: str = _GLADOS_LOGS
    data: str = _GLADOS_DATA
    assets: str = _GLADOS_ASSETS


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
    engine_audio_default: bool = True


class WeatherGlobal(BaseModel):
    latitude: float = 0.0
    longitude: float = 0.0
    temperature_unit: str = "fahrenheit"
    wind_speed_unit: str = "mph"
    auto_from_ha: bool = True


class GlobalConfig(BaseModel):
    home_assistant: HomeAssistantGlobal = HomeAssistantGlobal()
    network: NetworkGlobal = NetworkGlobal()
    paths: PathsGlobal = PathsGlobal()
    ssl: SSLGlobal = SSLGlobal()
    auth: AuthGlobal = AuthGlobal()
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
    gladys_api: ServiceEndpoint = ServiceEndpoint(
        url="http://localhost:8020"
    )


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
    silence_between_sentences_ms: int = 400
    sample_rate: int = 24000


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


class PersonalityConfig(BaseModel):
    default_tts: TTSParams = TTSParams()
    hexaco: HEXACOPersonality = HEXACOPersonality()
    emotion: EmotionPersonality = EmotionPersonality()
    attitudes: list[AttitudeEntry] = []
    preprompt: list[PrepromptEntry] = []


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


class ObserverEntityRule(BaseModel):
    entity_id: str
    category: str = "notable"
    alert: bool = False


class ObserverConfig(BaseModel):
    enabled: bool = True
    entity_whitelist: list[ObserverEntityRule] = []
    judgment_model: str = "qwen2.5:14b-instruct-q4_K_M"
    judgment_ollama_url: str = _env("OLLAMA_AUTONOMY_URL",
                                    _env("OLLAMA_URL", "http://ollama:11434"))
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
