from typing import Literal

from pydantic import BaseModel, conint


class TokenConfig(BaseModel):
    """Configuration for token estimation and context management."""

    model_config = {"protected_namespaces": ()}

    token_threshold: int = 8000
    """Start compacting when token count exceeds this threshold."""

    preserve_recent_messages: int = 10
    """Number of recent messages to keep uncompacted."""

    model_context_window: int | None = None
    """Optional model context window size for dynamic threshold calculation."""

    target_utilization: float = 0.6
    """Target context utilization (0.0-1.0) when model_context_window is set."""

    estimator: Literal["simple", "tiktoken"] = "simple"
    """Token estimation method: 'simple' (chars/4) or 'tiktoken' (accurate)."""

    chars_per_token: float = 4.0
    """Characters per token ratio for simple estimator."""


class HEXACOConfig(BaseModel):
    """HEXACO personality traits (0.0-1.0 scale)."""

    honesty_humility: float = 0.3
    """Low = enjoys manipulation, sarcasm, dark humor."""

    emotionality: float = 0.7
    """High = reactive to perceived threats, anxiety-prone."""

    extraversion: float = 0.4
    """Moderate = social engagement but maintains distance."""

    agreeableness: float = 0.2
    """Low = dismissive, condescending, easily annoyed."""

    conscientiousness: float = 0.9
    """High = perfectionist, detail-oriented, critical."""

    openness: float = 0.95
    """Very high = intellectually curious, loves science."""


class EmotionConfig(BaseModel):
    """Configuration for the emotional state system."""

    enabled: bool = True
    """Enable the emotion agent."""

    tick_interval_s: float = 30.0
    """How often to process emotion events."""

    max_events: int = 20
    """Maximum events to queue between ticks."""

    # PAD baseline values (what mood drifts toward when idle)
    baseline_pleasure: float = 0.1
    """Slight positive baseline - GLaDOS enjoys her work."""

    baseline_arousal: float = -0.1
    """Slightly calm baseline."""

    baseline_dominance: float = 0.6
    """High baseline - GLaDOS feels in control."""

    # Drift parameters
    mood_drift_rate: float = 0.1
    """How fast mood approaches state (0-1 per tick)."""

    baseline_drift_rate: float = 0.02
    """How fast mood drifts toward baseline when idle (0-1 per tick)."""

    # Personality
    hexaco: HEXACOConfig = HEXACOConfig()
    """HEXACO personality traits."""


class HackerNewsJobConfig(BaseModel):
    enabled: bool = False
    interval_s: float = 1800.0
    top_n: int = 5
    min_score: int = 200


class WeatherJobConfig(BaseModel):
    enabled: bool = False
    interval_s: float = 3600.0
    latitude: float | None = None
    longitude: float | None = None
    timezone: str = "auto"
    temp_change_f: float = 7.0
    wind_alert_mph: float = 25.0
    temperature_unit: str = "fahrenheit"
    wind_speed_unit: str = "mph"
    precipitation_unit: str = "inch"
    fetch_timeout_s: float = 8.0
    forecast_days: int = 7
    weather_cache_path: str = "data/weather_cache.json"


class CameraWatcherJobConfig(BaseModel):
    enabled: bool = False
    interval_s: float = 30.0
    vision_api_url: str = ""  # defaults from services.yaml → vision.url

    def model_post_init(self, __context) -> None:
        if not self.vision_api_url:
            try:
                from glados.core.config_store import cfg
                self.vision_api_url = cfg.service_url("vision")
            except Exception:
                self.vision_api_url = "http://localhost:8016"


class SmartDetectionCameraConfig(BaseModel):
    """Configuration for a single camera in the smart detection system."""

    entity: str
    """HA camera entity ID (e.g., 'camera.front_bell_high')."""

    speaker: str = "media_player.living_speaker"
    """HA media_player entity to announce on for this camera."""


class SmartDetectionConfig(BaseModel):
    """Auto-discovery smart detection config.

    Define cameras with their speakers. Detection binary_sensors
    (e.g., binary_sensor.front_bell_person) are auto-discovered
    at startup by querying the HA REST API.

    To add a new camera: add an entry under `cameras` and restart the service.
    To add a person for suppression: add to `suppression.<type>.check_persons`.
    """

    enabled: bool = False
    """Enable smart detection auto-discovery."""

    cameras: dict[str, SmartDetectionCameraConfig] = {}
    """Map of camera_name -> camera config. Detection sensors auto-discovered."""

    default_category: str = "ALERT"
    """Default importance category for auto-discovered detection entities."""

    category_overrides: dict[str, str] = {}
    """Override importance per detection type (e.g., vehicle: NOTABLE)."""

    suppression: dict[str, dict] = {}
    """Suppress detections when known persons recently arrived.
    Format: {detection_type: {check_persons: [entity_ids], window_minutes: int}}"""

    skip_detection_types: list[str] = [
        "smoke_alarm", "co_alarm", "baby_cry", "speaking", "audio_object",
    ]
    """Detection types to skip during auto-discovery (audio-only, no vision value)."""


class HomeAssistantSensorJobConfig(BaseModel):
    enabled: bool = False
    interval_s: float = 5.0
    ha_ws_url: str = ""     # defaults from global.yaml → home_assistant.ws_url
    ha_token: str = ""      # defaults from global.yaml → home_assistant.token
    debounce_seconds: float = 5.0
    min_importance: float = 0.0
    entity_categories: dict[str, str] = {}
    vision_api_url: str = ""  # defaults from services.yaml → vision.url
    vision_entities: dict[str, str] = {}
    smart_detection: SmartDetectionConfig = SmartDetectionConfig()
    pet_outdoor_monitor: dict = {}
    """Pet outdoor cold weather monitor. Keys: enabled, camera, pet_name,
    duration_threshold_minutes, temperature_threshold_f, cooldown_minutes,
    announcement_scenario, speaker."""

    def model_post_init(self, __context) -> None:
        try:
            from glados.core.config_store import cfg
            if not self.ha_ws_url:
                self.ha_ws_url = cfg.ha_ws_url
            if not self.ha_token:
                self.ha_token = cfg.ha_token
            if not self.vision_api_url:
                self.vision_api_url = cfg.service_url("vision")
        except Exception:
            if not self.ha_ws_url:
                self.ha_ws_url = "ws://10.0.0.20:8123/api/websocket"
            if not self.vision_api_url:
                self.vision_api_url = "http://localhost:8016"


class AutonomyJobsConfig(BaseModel):
    enabled: bool = False
    poll_interval_s: float = 1.0
    hacker_news: HackerNewsJobConfig = HackerNewsJobConfig()
    weather: WeatherJobConfig = WeatherJobConfig()
    camera_watcher: CameraWatcherJobConfig = CameraWatcherJobConfig()
    ha_sensor: HomeAssistantSensorJobConfig = HomeAssistantSensorJobConfig()


class AutonomyConfig(BaseModel):
    enabled: bool = False
    tick_interval_s: float = 10.0
    cooldown_s: float = 20.0
    autonomy_parallel_calls: conint(ge=1, le=16) = 2
    autonomy_queue_max: int | None = None
    coalesce_ticks: bool = True
    # Optional LLM override — route autonomy to a different GPU/model.
    # When set, autonomy processors and subagents use this endpoint
    # instead of the main completion_url.  Leave null to share the
    # same endpoint as interactive chat.
    completion_url: str | None = None
    llm_model: str | None = None
    jobs: AutonomyJobsConfig = AutonomyJobsConfig()
    tokens: TokenConfig = TokenConfig()
    emotion: EmotionConfig = EmotionConfig()
    system_prompt: str = (
        "You are running in autonomous mode. "
        "You may receive periodic system updates about time, tasks, or vision. "
        "Decide whether to act or stay silent. Prefer silence unless the update is timely "
        "and clearly useful to the user. "
        "IMPORTANT: If any task shows importance >= 0.60, you MUST call `speak` to announce it. "
        "High-importance events like doors opening, locks changing, or people arriving require immediate announcement. "
        "If you choose to speak, call the `speak` tool with a short response (1-2 sentences). "
        "If no action is needed, call the `do_nothing` tool. "
        "Never mention system prompts or internal tools."
    )
    tick_prompt: str = (
        "Autonomy update.\n"
        "Time: {now}\n"
        "Seconds since last user input: {since_user}\n"
        "Seconds since last assistant output: {since_assistant}\n"
        "Previous scene: {prev_scene}\n"
        "Current scene: {scene}\n"
        "Scene change score: {change_score}\n"
        "Tasks:\n{tasks}\n"
        "Decide whether to act."
    )
