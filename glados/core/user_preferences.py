"""UserPreferences — user-level knobs the CommandResolver consults.

These are the choices a user shouldn't have to rephrase every time:
which light tier they prefer (lamps over overheads), which areas are
"task" areas (kitchen counters, desk, workbench — overheads are welcome
there), named aliases for rooms HA doesn't know by that name
("ResidentB's office"), default color temperature and brightness targets,
and how big a step "brighter" / "a little brighter" / "a lot brighter"
each represent.

Everything per-entity (e.g., which light is a lamp vs an overhead)
lives in HA as entity labels — NOT here. See CURRENT_STATE.md §Q2.
This model only holds things HA can't know: the user's subjective
preferences and the aliases they use.

Persistence: YAML at `configs/user_preferences.yaml`. Loaded on startup
by the engine; reloaded via `load_user_preferences()` when the WebUI
saves an edit. No hot-reload watcher — the WebUI save path calls the
reload function directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import BaseModel, Field, field_validator


# Default tier priority — "prefer indirect / ambient light before
# overheads". Order is significant: the resolver picks entities in this
# order when executing an ambiguous "turn on the lights" in a non-task
# area. Extend in the usual Phase 6 pattern (defaults here + YAML
# override + WebUI edit).
DEFAULT_TIER_PRIORITY: tuple[str, ...] = (
    "lamp",
    "accent",
    "task",
    "overhead",
    "flood",
    "strip",
)

# Canonical set of valid tiers. Matches what we expect as HA labels on
# each light entity. Keep in sync with the HA label registry; anything
# outside this set is either a typo or a new concept that should be
# added explicitly.
VALID_TIERS: frozenset[str] = frozenset(DEFAULT_TIER_PRIORITY)

# Default task-area list — rooms where the user actually wants overheads
# on. The rewrite CSV documents why: kitchen counters, desk work,
# bathroom vanity, garage workbench, laundry folding.
DEFAULT_TASK_AREAS: tuple[str, ...] = (
    "kitchen",
    "garage_workbench",
    "office_desk",
    "bathroom_vanity",
    "laundry",
)


class UserPreferences(BaseModel):
    """User-level resolution preferences. Pydantic model so YAML
    parsing + validation is one step. All fields have defaults so an
    operator can start with an empty file and customize incrementally.
    """

    model_config = {"protected_namespaces": (), "extra": "forbid"}

    lighting_tier_priority: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TIER_PRIORITY),
        description=(
            "Ordered list of light tiers the resolver prefers, most "
            "preferred first. Used when 'turn on the lights' hits an "
            "area that isn't a task area: the resolver picks the "
            "highest-priority tier present in that area."
        ),
    )

    task_areas: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TASK_AREAS),
        description=(
            "HA area_ids where overheads/task lighting is acceptable "
            "or desired. In these areas, the resolver doesn't apply "
            "lamp-first preference."
        ),
    )

    area_aliases: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map from spoken alias to HA area_id. For rooms HA doesn't "
            "call by the name the user uses — e.g. "
            "{'cindys office': 'residentb_office', 'the workshop': 'garage'}. "
            "Keys are lowercased and whitespace-normalized by the "
            "validator so matching can be straightforward."
        ),
    )

    default_warm_kelvin: int = Field(
        default=2700,
        ge=1500,
        le=6500,
        description="Kelvin target for 'warm white' / 'cozy' requests.",
    )

    default_cool_kelvin: int = Field(
        default=5000,
        ge=1500,
        le=6500,
        description="Kelvin target for 'daylight' / 'cool white' requests.",
    )

    default_normal_brightness_pct: int = Field(
        default=60,
        ge=1,
        le=100,
        description=(
            "Brightness used when turning on a light without a level "
            "specified and when 'reset the lights' / 'normal lights' "
            "fires."
        ),
    )

    brightness_step_pct: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Default step for 'brighter' / 'dimmer' / 'turn up'.",
    )

    brightness_step_small_pct: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Step for 'a little brighter' / 'a little dimmer'.",
    )

    brightness_step_large_pct: int = Field(
        default=50,
        ge=1,
        le=100,
        description="Step for 'a lot brighter' / 'way too bright'.",
    )

    # ---- Validators --------------------------------------------------

    @field_validator("lighting_tier_priority")
    @classmethod
    def _validate_tiers(cls, v: list[str]) -> list[str]:
        """Reject unknown tier names and duplicates. Order is preserved.

        An unknown tier in the preferences is almost certainly a typo
        ('lamps' vs 'lamp') that would silently skip a user's intended
        priority — so we fail loud at load time rather than at first
        use. Same reasoning for duplicates: they'd make the user think
        the second instance mattered when in practice only the first
        ordering position counts.
        """
        seen: set[str] = set()
        out: list[str] = []
        for tier in v:
            if tier not in VALID_TIERS:
                raise ValueError(
                    f"Unknown lighting tier {tier!r}. "
                    f"Valid values: {sorted(VALID_TIERS)}."
                )
            if tier in seen:
                raise ValueError(f"Duplicate tier {tier!r} in lighting_tier_priority.")
            seen.add(tier)
            out.append(tier)
        return out

    @field_validator("area_aliases")
    @classmethod
    def _normalize_aliases(cls, v: dict[str, str]) -> dict[str, str]:
        """Lowercase keys and strip whitespace so a 'ResidentB's Office'
        entry and a 'cindys office' utterance both match."""
        normalized: dict[str, str] = {}
        for alias, area_id in v.items():
            key = " ".join(str(alias).lower().strip().split())
            if not key:
                continue
            target = str(area_id).strip()
            if not target:
                raise ValueError(
                    f"area_aliases[{alias!r}] maps to an empty area_id."
                )
            normalized[key] = target
        return normalized

    # ---- Queries the resolver uses ----------------------------------

    def is_task_area(self, area_id: str | None) -> bool:
        """True when the given area_id is a task area — where overheads
        are an acceptable or preferred default."""
        if not area_id:
            return False
        return area_id in self.task_areas

    def resolve_area_alias(self, phrase: str | None) -> str | None:
        """Look up a user-spoken phrase in the alias map. Returns the
        HA area_id, or None if no alias matches. Matching is
        case-/whitespace-insensitive on the lookup side."""
        if not phrase:
            return None
        key = " ".join(phrase.lower().strip().split())
        return self.area_aliases.get(key)


# ---------------------------------------------------------------------------
# YAML load / save
# ---------------------------------------------------------------------------

def load_user_preferences(path: str | Path) -> UserPreferences:
    """Load preferences from `path`. Missing file → all-defaults.

    An empty or missing file is a valid and common state — most users
    will customize a few fields in the WebUI and leave the rest at
    defaults. Returning defaults rather than raising lets the engine
    start up cleanly.
    """
    p = Path(path)
    if not p.exists():
        logger.info(
            "UserPreferences: {} not found, using defaults",
            p,
        )
        return UserPreferences()

    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("UserPreferences: cannot read {}: {}", p, exc)
        return UserPreferences()

    if not text.strip():
        return UserPreferences()

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        logger.error("UserPreferences: YAML parse error in {}: {}", p, exc)
        raise

    if not isinstance(data, dict):
        raise ValueError(
            f"UserPreferences YAML at {p} must be a mapping, got "
            f"{type(data).__name__}."
        )

    return UserPreferences.model_validate(data)


def save_user_preferences(prefs: UserPreferences, path: str | Path) -> None:
    """Write preferences to `path` as YAML. Creates parent dirs.

    Used by the WebUI save path. Writes atomically via a temp file +
    rename so a crashed writer never leaves a half-parsed file that
    blocks engine startup.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = prefs.model_dump()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    tmp.replace(p)
