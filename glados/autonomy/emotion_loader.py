"""
Loader for emotion_config.yaml.

Provides a single load_emotion_config() call that reads the YAML file
and returns typed dataclasses. All other emotion modules import from here.

Platform note: Uses pathlib throughout — works on Windows and Linux.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger


@dataclass
class EmotionBaseline:
    pleasure:  float = 0.1
    arousal:   float = -0.1
    dominance: float = 0.6


@dataclass
class EmotionDrift:
    normal_rate:     float = 0.02
    locked_rate:     float = 0.002
    mood_drift_rate: float = 0.1


@dataclass
class EmotionCooldown:
    duration_hours:      float = 3.0
    pleasure_threshold:  float = -0.5
    arousal_threshold:   float = 0.6


@dataclass
class EmotionEvents:
    max_events:     int   = 20
    debounce_s:     float = 15.0
    idle_timeout_s: float = 21600.0


@dataclass
class EmotionHEXACO:
    honesty_humility:  float = 0.3
    emotionality:      float = 0.7
    extraversion:      float = 0.4
    agreeableness:     float = 0.2
    conscientiousness: float = 0.9
    openness:          float = 0.95


@dataclass
class EmotionRegion:
    """A named PAD region — maps a cube in PAD space to an emotion name."""
    name:  str
    p_min: float
    p_max: float
    a_min: float
    a_max: float
    d_min: float
    d_max: float

    def matches(self, p: float, a: float, d: float) -> bool:
        return (self.p_min <= p <= self.p_max and
                self.a_min <= a <= self.a_max and
                self.d_min <= d <= self.d_max)

    def score(self, p: float, a: float, d: float) -> float:
        """Normalized centrality score 0-1. Higher = closer to center."""
        pc = (self.p_min + self.p_max) / 2
        ac = (self.a_min + self.a_max) / 2
        dc = (self.d_min + self.d_max) / 2
        pr = max(self.p_max - self.p_min, 0.001) / 2
        ar = max(self.a_max - self.a_min, 0.001) / 2
        dr = max(self.d_max - self.d_min, 0.001) / 2
        return (
            (1.0 - abs(p - pc) / pr) +
            (1.0 - abs(a - ac) / ar) +
            (1.0 - abs(d - dc) / dr)
        ) / 3.0


@dataclass
class SeverityLevel:
    repeats: int
    label: str
    description: str


@dataclass
class EscalationConfig:
    similarity_threshold: float = 0.75
    history_window: int = 6
    curve: str = "exponential"
    curve_exponent: float = 1.5
    severity_levels: list[SeverityLevel] = field(default_factory=lambda: [
        SeverityLevel(1, "minor",      "first occurrence"),
        SeverityLevel(2, "notable",    "asked this before — mild irritation warranted"),
        SeverityLevel(3, "escalating", "third time asking essentially the same thing — this is annoying"),
        SeverityLevel(4, "severe",     "fourth repetition — she has clearly stopped listening"),
        SeverityLevel(5, "critical",   "five or more times — full hostility response warranted"),
    ])

    def severity_for(self, repeat_count: int) -> SeverityLevel:
        """Return the highest matching severity level for this repeat count."""
        matched = self.severity_levels[0]
        for level in self.severity_levels:
            if repeat_count >= level.repeats:
                matched = level
        return matched

    def weight(self, repeat_count: int) -> float:
        """
        Severity weight 0.0-1.0 based on repeat count and curve shape.
        Used to scale event magnitude description for the LLM.
        """
        if repeat_count <= 1:
            return 0.0
        n = repeat_count - 1  # number of repeats beyond first
        max_n = max(lv.repeats for lv in self.severity_levels) - 1
        if self.curve == "exponential":
            raw = (n ** self.curve_exponent) / (max_n ** self.curve_exponent)
        elif self.curve == "stepped":
            raw = n / max_n if n >= max_n // 2 else (n / max_n) * 0.3
        else:  # linear
            raw = n / max_n
        return round(min(1.0, raw), 3)


@dataclass
class EmotionConfig:
    baseline:   EmotionBaseline   = field(default_factory=EmotionBaseline)
    drift:      EmotionDrift      = field(default_factory=EmotionDrift)
    cooldown:   EmotionCooldown   = field(default_factory=EmotionCooldown)
    events:     EmotionEvents     = field(default_factory=EmotionEvents)
    escalation: EscalationConfig  = field(default_factory=EscalationConfig)
    hexaco:     EmotionHEXACO     = field(default_factory=EmotionHEXACO)
    emotions:   list[EmotionRegion] = field(default_factory=list)


_DEFAULT_CONFIG_PATH = Path("configs/emotion_config.yaml")
_cached: EmotionConfig | None = None


def load_emotion_config(path: str | Path | None = None) -> EmotionConfig:
    """Load emotion config from YAML. Cached after first load."""
    global _cached
    if _cached is not None:
        return _cached

    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        logger.warning("emotion_config: {} not found — using defaults", config_path)
        _cached = EmotionConfig()
        return _cached

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

        b = raw.get("baseline", {})
        baseline = EmotionBaseline(
            pleasure  = float(b.get("pleasure",  0.1)),
            arousal   = float(b.get("arousal",  -0.1)),
            dominance = float(b.get("dominance", 0.6)),
        )

        d = raw.get("drift", {})
        drift = EmotionDrift(
            normal_rate     = float(d.get("normal_rate",     0.02)),
            locked_rate     = float(d.get("locked_rate",     0.002)),
            mood_drift_rate = float(d.get("mood_drift_rate", 0.1)),
        )

        c = raw.get("cooldown", {})
        cooldown = EmotionCooldown(
            duration_hours     = float(c.get("duration_hours",     3.0)),
            pleasure_threshold = float(c.get("pleasure_threshold", -0.5)),
            arousal_threshold  = float(c.get("arousal_threshold",   0.6)),
        )

        e = raw.get("events", {})
        events = EmotionEvents(
            max_events     = int(e.get("max_events",     20)),
            debounce_s     = float(e.get("debounce_s",     15.0)),
            idle_timeout_s = float(e.get("idle_timeout_s", 21600.0)),
        )

        h = raw.get("hexaco", {})
        hexaco = EmotionHEXACO(
            honesty_humility  = float(h.get("honesty_humility",  0.3)),
            emotionality      = float(h.get("emotionality",      0.7)),
            extraversion      = float(h.get("extraversion",      0.4)),
            agreeableness     = float(h.get("agreeableness",     0.2)),
            conscientiousness = float(h.get("conscientiousness", 0.9)),
            openness          = float(h.get("openness",          0.95)),
        )

        regions = []
        for em in raw.get("emotions", []):
            p = em.get("p", [-1.0, 1.0])
            a = em.get("a", [-1.0, 1.0])
            d_range = em.get("d", [-1.0, 1.0])
            regions.append(EmotionRegion(
                name  = str(em.get("name", "Unknown")),
                p_min = float(p[0]), p_max = float(p[1]),
                a_min = float(a[0]), a_max = float(a[1]),
                d_min = float(d_range[0]), d_max = float(d_range[1]),
            ))

        esc_raw = raw.get("escalation", {})
        sev_levels = []
        for sv in esc_raw.get("severity_levels", []):
            sev_levels.append(SeverityLevel(
                repeats     = int(sv.get("repeats", 1)),
                label       = str(sv.get("label", "minor")),
                description = str(sv.get("description", "")),
            ))
        escalation = EscalationConfig(
            similarity_threshold = float(esc_raw.get("similarity_threshold", 0.75)),
            history_window       = int(esc_raw.get("history_window", 6)),
            curve                = str(esc_raw.get("curve", "exponential")),
            curve_exponent       = float(esc_raw.get("curve_exponent", 1.5)),
            severity_levels      = sev_levels or EscalationConfig().severity_levels,
        )

        _cached = EmotionConfig(
            baseline=baseline,
            drift=drift,
            cooldown=cooldown,
            events=events,
            escalation=escalation,
            hexaco=hexaco,
            emotions=regions,
        )
        logger.info("emotion_config: loaded {} emotions from {}", len(regions), config_path)

    except Exception as exc:
        logger.error("emotion_config: failed to load {}: {} — using defaults", config_path, exc)
        _cached = EmotionConfig()

    return _cached


def reload_emotion_config(path: str | Path | None = None) -> EmotionConfig:
    """Force reload from disk (e.g. after editing the file)."""
    global _cached
    _cached = None
    return load_emotion_config(path)


def classify_emotion(p: float, a: float, d: float) -> tuple[str, float]:
    """
    Translate PAD values to the closest named GLaDOS emotion + intensity.
    Reads emotion regions from emotion_config.yaml.
    Falls back to 'Contemptuous Calm' if no region matches.
    """
    cfg = load_emotion_config()
    best_name = "Contemptuous Calm"
    best_score = -1.0

    for region in cfg.emotions:
        if not region.matches(p, a, d):
            continue
        score = region.score(p, a, d)
        if score > best_score:
            best_score = score
            best_name = region.name

    intensity = round(max(0.0, min(1.0, best_score)), 2) if best_score >= 0 else 0.5
    return best_name, intensity
