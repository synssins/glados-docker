"""Phase 8.7b — quip selector.

The composer replaces the LLM's speech output with a pre-written,
Portal-voice one-liner picked from a directory tree on disk. This
eliminates three failure modes the LLM exhibits under prompt drift:

  - Device-name leaks ("I turned off light.floor_lamp_one")
  - Language drift (mid-reply lapses into JSON, French, etc.)
  - Verb-polarity flips ("Activating..." when we just deactivated)

The selector is a pure function of (event_category, intent, outcome,
mood, entity_count, time_of_day). It never calls the LLM, never hits
HA, and never emits a device friendly-name — the only substitutions
allowed are count ("all three"), scene name (scene entities carry
human labels anyway), and outcome modifier ("already asleep").

Directory structure:

  configs/quips/
    command_ack/
      turn_on/
        normal.txt
        cranky.txt
        amused.txt
        evening.txt
      turn_off/
      brightness_up/
      brightness_down/
      color_change/
      scene_activate/
    query_answer/
      state_query/
      environmental/
      status/
      time/
    ambient_cue/
      too_dark/
      too_bright/
      reading/
      movie/
      dinner/
    outcome_modifier/
      partial_success.txt
      already_in_state.txt
      no_such_entity.txt
      unavailable_entity.txt
    global/
      acknowledgement.txt
      void_references.txt

One line per quip. Blank lines and lines starting with `#` ignored.

Selection walks from most-specific to most-general:
  1. <event_category>/<intent>/<mood_file>
  2. <event_category>/<intent>/normal.txt
  3. <event_category>/<intent>/ (any file)
  4. global/acknowledgement.txt

Uniform random pick from the matching file. When the fallback chain
exhausts without a file, returns empty string — caller keeps the
LLM's original speech so we degrade gracefully when the library
isn't deployed yet.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# Supported event categories. Keep tight — adding a new category
# means creating the directory + seeding files, so accepting only
# these four prevents typos from silently missing their library.
EventCategory = str  # "command_ack" | "query_answer" | "ambient_cue" | "error"
VALID_CATEGORIES: frozenset[str] = frozenset({
    "command_ack", "query_answer", "ambient_cue", "error",
})


@dataclass(frozen=True)
class QuipRequest:
    """Everything the selector needs to pick a line.

    Kept frozen so the composer can cache or hash requests if we
    ever want to dedupe back-to-back picks. Fields are deliberately
    coarse: the selector matches by exact string against directory
    names, so callers are expected to normalize before asking."""
    event_category: EventCategory
    intent: str                     # "turn_on", "turn_off", "brightness_up", ...
    outcome: str = "success"        # "success" | "partial" | "already_in_state" | ...
    mood: str = "normal"            # "normal" | "cranky" | "amused" | "evening"
    entity_count: int = 1
    time_of_day: str = ""           # "", "morning", "afternoon", "evening", "night"


@dataclass
class QuipLibrary:
    """In-memory snapshot of the on-disk quip files.

    Callers construct once at startup with `QuipLibrary.load(root)`
    and pass the same instance to every `pick()` call. Thread-safe
    for concurrent reads — the dict is built at load time and never
    mutated at query time."""
    root: Path
    _lines: dict[Path, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, root: str | Path) -> "QuipLibrary":
        """Walk `root`, read every `.txt` file. Missing root is not
        an error — returns an empty library so the composer falls
        back to LLM speech cleanly when the operator hasn't deployed
        content yet."""
        root_path = Path(root)
        lib = cls(root=root_path)
        if not root_path.exists():
            return lib
        for p in root_path.rglob("*.txt"):
            try:
                raw = p.read_text(encoding="utf-8")
            except OSError:
                continue
            lines = [
                ln.strip()
                for ln in raw.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")
            ]
            if lines:
                lib._lines[p] = lines
        return lib

    def is_empty(self) -> bool:
        return not self._lines

    def _candidates(
        self, req: QuipRequest, mood_override: str | None = None,
    ) -> list[str]:
        """Build the fallback chain and return the first file that
        has at least one line. Empty list when nothing matches."""
        mood = mood_override or req.mood or "normal"
        # Most-specific → most-general. Each path is relative to self.root.
        candidates: list[Path] = [
            # 1. Exact match: category / intent / mood.txt
            self.root / req.event_category / req.intent / f"{mood}.txt",
            # 2. time-of-day variant: category / intent / <tod>.txt
        ]
        if req.time_of_day:
            candidates.append(
                self.root / req.event_category / req.intent / f"{req.time_of_day}.txt"
            )
        candidates.extend([
            # 3. Intent's default mood.
            self.root / req.event_category / req.intent / "normal.txt",
            # 4. Category-level file named for the intent (flat layout).
            self.root / req.event_category / f"{req.intent}.txt",
            # 5. Category default.
            self.root / req.event_category / "default.txt",
        ])
        for c in candidates:
            lines = self._lines.get(c)
            if lines:
                return lines
        # 6. Global acknowledgement — last resort.
        return self._lines.get(
            self.root / "global" / "acknowledgement.txt", []
        )

    def pick(
        self,
        req: QuipRequest,
        *,
        rng: random.Random | None = None,
    ) -> str:
        """Return one quip for the request. Empty string means no
        library content matched — the composer keeps the LLM speech."""
        if req.event_category not in VALID_CATEGORIES:
            return ""
        cands = self._candidates(req)
        if not cands:
            # Try mood=normal as a second attempt before giving up.
            cands = self._candidates(req, mood_override="normal")
        if not cands:
            return ""
        r = rng or random
        return r.choice(cands)


# ---------------------------------------------------------------------------
# Mood mapping from the existing HEXACO + emotion affect vector.
# Plan §8.7b spec:
#   anger > 0.6 → cranky
#   joy > 0.6   → amused
#   else        → normal
# Kept defensive: None inputs, missing keys, non-numeric values all
# resolve to "normal" so a broken affect source never blocks replies.
# ---------------------------------------------------------------------------


def mood_from_affect(affect: dict[str, float] | None) -> str:
    if not affect or not isinstance(affect, dict):
        return "normal"
    try:
        if float(affect.get("anger", 0)) > 0.6:
            return "cranky"
        if float(affect.get("joy", 0)) > 0.6:
            return "amused"
    except (TypeError, ValueError):
        return "normal"
    return "normal"


# ---------------------------------------------------------------------------
# Helpers for the composer's substitution rules. Device friendly-names
# are FORBIDDEN — only count and outcome-modifier are allowed.
# ---------------------------------------------------------------------------


def format_entity_count(n: int) -> str:
    """Produce a Portal-voice count phrase. Never a device name."""
    if n <= 0:
        return ""
    if n == 1:
        return "one"
    if n == 2:
        return "both"
    if n == 3:
        return "all three"
    if n <= 10:
        return f"all {n}"
    return "the entire set"


__all__ = [
    "EventCategory",
    "VALID_CATEGORIES",
    "QuipLibrary",
    "QuipRequest",
    "format_entity_count",
    "mood_from_affect",
]
