"""Tier 2 — LLM disambiguation.

Invoked when HA's conversation API misses with `should_disambiguate`.
Builds a constrained prompt with candidate entities (filtered by
domain hint, current state, and per-domain fuzzy thresholds), asks
the LLM for a structured JSON decision, and either:

  - executes the chosen service via the HA WebSocket call_service, OR
  - returns a clarifying question in persona voice, OR
  - refuses with a persona-voiced denial when the intent allowlist
    blocks the requested domain for the requesting source.

Uses Ollama's autonomy endpoint (T4 CUDA) for speed; the prompt is
short and the structured JSON output bounds runaway generation.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from glados.observability import AuditEvent, audit

from loguru import logger

from glados.ha import HAClient, get_cache, get_client
from glados.ha.entity_cache import CandidateMatch, EntityCache, EntityState
from glados.persona.rewriter import strip_trailing_vocative

from .rules import (
    DisambiguationRules,
    IntentAllowlist,
    domain_filter_for_utterance,
    looks_like_home_command,
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class DisambiguationResult:
    """Outcome of one Tier 2 attempt.

    `handled` is True when the disambiguator produced a final user-
    facing reply (executed an action, asked for clarification, or
    refused). `should_fall_through` means Tier 3 (full LLM with all
    tools) should take over."""

    handled: bool
    should_fall_through: bool
    speech: str
    decision: str = ""                  # "execute" | "clarify" | "refuse" | "fall_through"
    entity_ids: list[str] = field(default_factory=list)
    service: str = ""
    service_data: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    candidates_shown: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0
    llm_raw: str = ""                   # For audit trace


# ---------------------------------------------------------------------------
# Disambiguator
# ---------------------------------------------------------------------------

# Ollama call timeout. Tunable via DISAMBIGUATOR_TIMEOUT_S env.
# Default is 45s — covers 14B JSON generation on a single-GPU unified
# deployment (B60 + IPEX: prefill ~10s for ~3000-token prompt,
# generation ~15-25s for a full JSON response). Falling through to
# Tier 3 is worse than waiting another 20s here; the priority gate
# already holds autonomy off while Tier 2 runs. Operators with faster
# hardware can lower this env var.
_LLM_TIMEOUT_S = float(os.environ.get("DISAMBIGUATOR_TIMEOUT_S", "45"))
# call_service ack timeout. 5s was too short — HA can take longer to
# return the WS confirmation when the target is a group entity that
# cascades to many members, or under load. The action is usually
# already in flight by the time HA acks (HA's WS API acks acceptance,
# not completion), so a missed ack within the window does NOT mean
# the action failed. Bumping to 15s reduces false 'silent failure'
# audit rows.
_HA_CALL_TIMEOUT_S = 15.0


_UNIVERSAL_QUANTIFIERS: frozenset[str] = frozenset({
    "all", "every", "everything", "everywhere",
    "whole", "entire", "total",
})


def _has_universal_quantifier(text: str) -> bool:
    """Detect 'all X', 'every X', 'whole house', etc. — phrases where
    the user clearly wants broad action across all matching entities.
    The disambiguator should execute decisively rather than asking
    'which one?' for these."""
    if not text:
        return False
    words = {w.strip(".,!?;:'\"").lower() for w in text.split()}
    return bool(words & _UNIVERSAL_QUANTIFIERS)


# ---------------------------------------------------------------------------
# Qualifier pre-filter — protects against the LLM substituting a
# different qualifier when state-based inference would otherwise
# eliminate the user's explicit target. See
# `docs/state-query-prompt-research.md` and the commit that landed
# the desk-lamp fix.
#
# The LLM gets a `coverage=X%` hint on each candidate already, but on
# a 14B CPU/single-GPU model that signal is out-ranked by the state-
# filter rule some fraction of the time. This server-side pre-filter
# is a belt around the prompt's suspender: if the user's utterance
# contains distinctive qualifier words AND any candidate contains
# all of them, we restrict the candidate list so the LLM physically
# cannot pick a different qualifier.
#
# Name-agnostic — works on whatever the user said vs. whatever is in
# their friendly_names. Empty-filter fallback preserves today's
# behavior when the user's qualifier isn't literally in any entity.
# ---------------------------------------------------------------------------

# Words we DO NOT treat as distinctive qualifiers. These are stop-
# words, action verbs, direction/quantity modifiers, and the generic
# head nouns ('light', 'lamp') that the domain filter already covers.
_QUALIFIER_STOPWORDS: frozenset[str] = frozenset({
    # Articles / pronouns
    "a", "an", "the", "this", "that", "these", "those",
    "it", "its", "them", "their", "one", "any", "some",
    # Be-verbs / auxiliaries
    "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "has", "have", "had", "can", "could",
    "would", "should", "will", "shall", "may", "might",
    # Home-control action verbs
    "turn", "set", "put", "make", "get", "give", "take",
    "dim", "brighten", "lower", "raise", "flip", "switch",
    "activate", "deactivate", "toggle", "enable", "disable",
    "open", "close", "lock", "unlock", "start", "stop",
    "pause", "resume", "play", "kill", "hit", "shut",
    # State / direction / intensity modifiers
    "on", "off", "up", "down", "higher", "bright", "dim",
    "brighter", "dimmer", "lighter", "darker",
    "warmer", "cooler", "hotter", "colder",
    "half", "double", "quarter", "third",
    "full", "maximum", "max", "min", "minimum",
    "normal", "default",
    # Prepositions / conjunctions
    "in", "at", "to", "from", "by", "of", "for", "with",
    "and", "or", "but", "if", "then", "than", "as",
    # Pleasantries / modifiers
    "please", "just", "only", "still", "really", "very",
    "too", "quite", "a", "bit", "little", "lot", "much",
    # Pronouns / self-references
    "i", "me", "my", "mine", "we", "us", "our",
    "you", "your", "yours",
    # Generic head nouns already covered by the domain filter —
    # keeping them OUT of qualifiers prevents filtering on words
    # every light entity contains.
    "light", "lights", "lamp", "lamps",
    "fan", "fans", "switch", "switches",
    "scene", "scenes", "cover", "covers",
    # Time / relative
    "now", "later", "today", "tonight", "tomorrow",
    "ago", "second", "seconds", "minute", "minutes",
    "hour", "hours",
})

# Extract word-like tokens. Keeps apostrophes ("cindy's") but strips
# other punctuation.
_WORD_RE = re.compile(r"[a-z']+")


def _extract_qualifiers(utterance: str) -> list[str]:
    """Return the user's distinctive qualifier words from an utterance.

    Returns words like 'desk', 'reading', 'office', 'porch',
    "cindy's". Excludes stopwords, verbs, modifiers, and the generic
    head nouns that every candidate of a domain would share.

    Length-1 tokens are dropped — they're usually noise ("a", "i").
    """
    if not utterance:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for word in _WORD_RE.findall(utterance.lower()):
        if len(word) < 2:
            continue
        if word in _QUALIFIER_STOPWORDS:
            continue
        if word in seen:
            continue
        seen.add(word)
        out.append(word)
    return out


def _candidate_search_text(c: CandidateMatch) -> str:
    """Flatten everything about a candidate that the qualifier filter
    might match against — friendly_name, entity_id, matched_name, and
    aliases. Lowercased for case-insensitive containment checks."""
    parts = [
        c.matched_name or "",
        c.entity.friendly_name or "",
        c.entity.entity_id or "",
    ]
    aliases = getattr(c.entity, "aliases", None)
    if aliases:
        parts.extend(aliases)
    return " ".join(parts).lower()


def _filter_by_qualifiers(
    candidates: list[CandidateMatch],
    qualifiers: list[str],
) -> list[CandidateMatch]:
    """Restrict candidates to those containing ALL of the user's
    distinctive qualifier words.

    Returns an empty list when no candidate contains every qualifier
    — caller is expected to fall back to the unfiltered list rather
    than dropping the turn. Name-agnostic: works against whatever
    the operator's friendly_names happen to be.
    """
    if not qualifiers or not candidates:
        return list(candidates)
    out: list[CandidateMatch] = []
    for c in candidates:
        text = _candidate_search_text(c)
        if all(q in text for q in qualifiers):
            out.append(c)
    return out


def _entity_search_text(entity: EntityState) -> str:
    """Flattened, lowercased text of an entity for qualifier lookup.
    Matches the same fields `_candidate_search_text` uses so the
    cache scan and the candidate filter agree."""
    aliases = getattr(entity, "aliases", None) or []
    return " ".join([
        entity.friendly_name or "",
        entity.entity_id or "",
        " ".join(aliases),
    ]).lower()


def _unwrap_llm_response(decision: dict[str, Any]) -> dict[str, Any]:
    """Peel off common LLM wrapper shapes that aren't in the schema.

    Observed on live qwen2.5:14b on 2026-04-19:
      {"response": {"type": "json", "data": {"actions": [...], ...}}}
    The real payload is nested under response.data. Unwrap it so the
    parser can see the action list. Never wraps more than once; this
    is a best-effort normalization for known drift patterns.
    """
    if not isinstance(decision, dict):
        return decision  # type: ignore[return-value]
    # {"response": {"data": {...}}}
    resp = decision.get("response")
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, dict):
            return data
    # {"result": {...}} — another variant seen occasionally
    result = decision.get("result")
    if isinstance(result, dict) and (
        "actions" in result or "decision" in result
        or ("entity_ids" in result and "service" in result)
    ):
        return result
    return decision


def _coerce_entity_ids(a: dict[str, Any]) -> list[str]:
    """Accept per-action entity references in several shapes.

    LLM drift patterns observed:
      - entity_ids: ["light.x", "light.y"]   (correct, list)
      - entity_id:  "light.x"                (singular string)
      - entity_id:  ["light.x"]              (singular field, list value)
      - entity:     "light.x"                (typo field name)
    """
    raw = a.get("entity_ids")
    if isinstance(raw, list):
        return [str(e) for e in raw if isinstance(e, str) and e]
    if isinstance(raw, str) and raw:
        return [raw]
    raw = a.get("entity_id")
    if isinstance(raw, str) and raw:
        return [raw]
    if isinstance(raw, list):
        return [str(e) for e in raw if isinstance(e, str) and e]
    raw = a.get("entity")
    if isinstance(raw, str) and raw:
        return [raw]
    return []


def _coerce_service(a: dict[str, Any]) -> str:
    """Accept per-action service in several shapes.

    LLM drift patterns observed:
      - service: "light.turn_on"     (correct)
      - action:  "turn_on"           (field renamed)
      - method:  "turn_on"           (another rename)
    """
    for key in ("service", "action", "method"):
        v = a.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _coerce_service_data(a: dict[str, Any]) -> dict[str, Any]:
    """Per-action service_data may arrive under its proper name or
    renamed to `data` / `params` / `parameters`."""
    for key in ("service_data", "data", "params", "parameters"):
        v = a.get(key)
        if isinstance(v, dict):
            return dict(v)
    return {}


def _parse_actions(decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the LLM's `execute` output into a list of actions.

    Supports two schemas + tolerant variants:
      - SHAPE 1 (legacy single-action): top-level `entity_ids` +
        `service` + `service_data`. Wrapped into a single-element
        list so downstream code only handles one shape.
      - SHAPE 2 (compound): `actions: [{service, entity_ids,
        service_data?}, ...]`. Kept verbatim, filtered to valid
        entries.

    Tolerant of common LLM drift:
      - Response may be wrapped in {"response": {"data": ...}} or
        {"result": ...} — unwrapped automatically.
      - Per-action service field may be `action` or `method`
        instead of `service`.
      - Per-action entity target may be `entity_id` (str or list)
        or `entity` instead of `entity_ids`.
      - service_data may arrive as `data`, `params`, or `parameters`.

    An entry is valid when it has a non-empty service string AND at
    least one entity id. Invalid entries are dropped. Returns an
    empty list when the LLM produced nothing usable — caller treats
    that as a missing-fields fall-through.
    """
    decision = _unwrap_llm_response(decision)
    out: list[dict[str, Any]] = []

    # SHAPE 2 path — preferred when `actions` list is present.
    raw_actions = decision.get("actions")
    if isinstance(raw_actions, list) and raw_actions:
        for a in raw_actions:
            if not isinstance(a, dict):
                continue
            svc = _coerce_service(a)
            if not svc:
                continue
            eids = _coerce_entity_ids(a)
            if not eids:
                continue
            out.append({
                "service": svc,
                "entity_ids": eids,
                "service_data": _coerce_service_data(a),
            })
        if out:
            return out
        # An `actions` list that contained entries but none parsed is
        # not a SHAPE 1 candidate — fall through to empty return.
        return out

    # SHAPE 1 fallback — top-level single-action fields (also with
    # the same tolerant field names).
    svc = _coerce_service(decision)
    eids = _coerce_entity_ids(decision)
    if not eids:
        # Try the top-level plural form explicitly (most common
        # legacy shape).
        raw = decision.get("entity_ids")
        if isinstance(raw, list):
            eids = [str(e) for e in raw if isinstance(e, str) and e]
    if svc and eids:
        out.append({
            "service": svc,
            "entity_ids": eids,
            "service_data": _coerce_service_data(decision),
        })
    return out


def _find_qualifier_matches(
    cache: EntityCache,
    qualifiers: list[str],
    domain_hint: list[str] | None,
) -> list[CandidateMatch]:
    """Scan the full entity cache for qualifier-matching entities
    and return them as synthetic high-score candidates.

    Two-pass strategy:
      1. **ALL-match**: entities containing every qualifier word.
         Precise — `desk lamp` finds only actual desk lamps.
      2. **ANY-match fallback**: if ALL-match is empty, entities
         containing at least one qualifier. Handles multi-area
         utterances like `office and front entryway lights` where
         no single entity contains every qualifier but each area
         has candidates matching one.

    Returns an **uncapped** list — when the user uses distinctive
    qualifiers, every matching entity should be visible to the LLM
    regardless of fuzzy-score saturation. The fuzzy-matcher's
    candidate_limit was the old bottleneck: a room with many
    same-named segments (WLED strips, LED banks) could saturate the
    top-N and push the user's actual target out of view.
    """
    if not qualifiers:
        return []
    all_matches: list[CandidateMatch] = []
    any_matches: list[CandidateMatch] = []
    for entity in cache.snapshot():
        if domain_hint and entity.domain not in domain_hint:
            continue
        text = _entity_search_text(entity)
        hit_count = sum(1 for q in qualifiers if q in text)
        if hit_count == 0:
            continue
        match = CandidateMatch(
            entity=entity,
            matched_name=entity.friendly_name or entity.entity_id,
            score=100.0 if hit_count == len(qualifiers) else 80.0,
            sensitive=False,
        )
        if hit_count == len(qualifiers):
            all_matches.append(match)
        else:
            any_matches.append(match)
    return all_matches if all_matches else any_matches


class Disambiguator:
    """Tier 2 entry point. Stateless; safe to call concurrently."""

    def __init__(
        self,
        ha_client: HAClient,
        cache: EntityCache,
        ollama_url: str,
        model: str,
        rules: DisambiguationRules | None = None,
        allowlist: IntentAllowlist | None = None,
    ) -> None:
        self._ha = ha_client
        self._cache = cache
        self._ollama_url = ollama_url.rstrip("/")
        self._model = model
        self._rules = rules or DisambiguationRules()
        self._allowlist = allowlist or IntentAllowlist()

    # ── Rule management ──────────────────────────────────────

    @property
    def rules(self) -> DisambiguationRules:
        """Current in-memory rules. Read-only snapshot; callers should
        treat the returned object as immutable."""
        return self._rules

    def replace_rules(self, new_rules: DisambiguationRules) -> None:
        """Hot-swap the rules instance. Atomic reference assignment; no
        lock needed because rules is read, not mutated, by `run()`."""
        self._rules = new_rules

    # ── Entry point ───────────────────────────────────────────

    def run(
        self,
        utterance: str,
        source: str,
        source_area: str | None = None,
        assume_home_command: bool = False,
        prior_entity_ids: list[str] | None = None,
        prior_service: str | None = None,
    ) -> DisambiguationResult:
        """Drive a single utterance through Tier 2.

        `source_area` is an optional HA area_id hint — e.g., the area
        the requesting voice satellite lives in. When provided, the
        fuzzy candidate lookup boosts entities in that area so "the
        reading lamp" said from a living-room satellite reliably
        resolves to the living-room reading lamp without a clarify
        round trip.

        `assume_home_command` tells the disambiguator the upstream
        caller has already proven this is a home command (typically
        via carry-over from a prior Tier 1/2 turn), so it should
        bypass its own `looks_like_home_command` precheck. Without
        this, a follow-up like "Increase the brightness by ten
        percent" — which has no device keyword — is rejected at the
        precheck before the LLM ever sees the candidates.

        `prior_entity_ids` / `prior_service` carry the exact entity
        target of the most recent Tier 1/2 exchange. When provided,
        those entities are injected as synthetic full-coverage
        candidates so the LLM can act on the implied target even
        when fuzzy matching on the current utterance finds nothing."""
        t0 = time.perf_counter()

        # 0. Home-command precheck. Without this, a conversational
        # utterance like "Say hello to my friend, his name is Alan"
        # gets fuzzy-matched against every entity in the house and
        # the LLM produces an ambiguity response. Tier 3 is the
        # right place to handle chitchat. Carry-over callers that
        # have already proven home-command intent bypass this gate.
        if not assume_home_command and not looks_like_home_command(utterance):
            return self._fall_through(
                "no_home_command_intent",
                utterance[:120], t0,
                utterance=utterance, source=source,
            )

        # 1. Pull candidates from the cache. Bump limit when the user
        # used a universal quantifier — they want broad action and the
        # default 12 truncates the candidate list well before the LLM
        # has enough to act on "all lights".
        is_universal = _has_universal_quantifier(utterance)
        domain_hint = domain_filter_for_utterance(utterance)
        cand_limit = max(self._rules.candidate_limit, 30) if is_universal \
                     else self._rules.candidate_limit
        candidates = self._cache.get_candidates(
            utterance,
            domain_filter=domain_hint,
            limit=cand_limit,
            source_area=source_area,
            opposing_token_pairs=(self._rules.opposing_token_pairs or None),
            twin_dedup=self._rules.twin_dedup,
        )

        # Carry-over injection: when the caller told us this is a
        # follow-up on a specific prior target, surface those entities
        # as high-ranking candidates even if the fuzzy lookup on the
        # current utterance missed them. This is what makes
        # "Increase the brightness by ten percent" act on the desk
        # lamp that was targeted in the previous turn.
        if assume_home_command and prior_entity_ids:
            prior_matches = self._lookup_prior_candidates(prior_entity_ids)
            # De-dup by entity_id; prior candidates win the slot.
            keep_ids = {c.entity.entity_id for c in prior_matches}
            candidates = prior_matches + [
                c for c in candidates if c.entity.entity_id not in keep_ids
            ]

        if not candidates:
            return self._fall_through(
                "no_candidates",
                f"no entities matched query={utterance!r} domains={domain_hint}",
                t0,
                utterance=utterance, source=source,
            )

        # 1b. Qualifier-authoritative cache scan. When the user gave
        # distinctive qualifier words ("desk lamp", "office and front
        # entryway"), the fuzzy matcher's top-N can saturate with
        # entities sharing a generic head noun (15 "Lamp Segment"
        # WLED entries all scoring 85.5) and push the real target
        # out of view before the LLM ever sees it. We bypass the
        # fuzzy limit: scan the full cache for every entity that
        # matches, ALL-qualifier first and ANY-qualifier as fallback.
        # Uncapped — tokens are cheap, wrong targets are not.
        qualifiers = _extract_qualifiers(utterance)
        if qualifiers:
            scan_matches = _find_qualifier_matches(
                self._cache, qualifiers, domain_hint,
            )
            if scan_matches:
                logger.debug(
                    "Tier 2 qualifier scan: fuzzy={} → scan={} "
                    "(qualifiers={}, first={})",
                    len(candidates), len(scan_matches), qualifiers,
                    [c.entity.entity_id for c in scan_matches][:6],
                )
                candidates = scan_matches

        # 2. State-freshness guard. If any candidate's state is older
        # than the budget, skip state-based filtering this turn — bad
        # state is worse than no state inference. (Stage 3 plan: act
        # on stale state silently produces wrong-device outcomes.)
        max_age = max(self._cache.age(c.entity.entity_id) for c in candidates)
        state_fresh = max_age <= self._rules.max_state_age_seconds

        # 3. Build the constrained LLM prompt and call the autonomy model.
        prompt_messages = self._build_prompt(
            utterance=utterance, source=source,
            candidates=candidates, state_fresh=state_fresh,
            prior_entity_ids=(
                list(prior_entity_ids)
                if assume_home_command and prior_entity_ids else None
            ),
            prior_service=(prior_service if assume_home_command else None),
        )
        try:
            raw = self._call_ollama(prompt_messages)
        except Exception as exc:
            return self._fall_through(
                "llm_call_failed", str(exc), t0,
                candidates=_summarize_candidates(candidates),
                utterance=utterance, source=source,
            )

        # 4. Parse the JSON response.
        decision = _safe_parse_json(raw)
        if decision is None:
            return self._fall_through(
                "llm_bad_json", raw[:200], t0,
                candidates=_summarize_candidates(candidates),
                utterance=utterance, source=source, llm_raw=raw,
            )

        # Unwrap common LLM wrapper shapes before field lookups
        # ({"response": {"data": {...}}}, {"result": {...}}) so the
        # rest of this method reads the actual payload.
        decision = _unwrap_llm_response(decision)

        action = str(decision.get("decision", "")).lower()
        # Defensive: when the LLM omits `decision` but includes an
        # `actions` list (or the legacy `entity_ids`+`service` pair),
        # infer `execute`. Live 14B-instruct occasionally drops the
        # decision key when emitting SHAPE 2 compound output.
        # Inferring is safer than falling through to Tier 3 chitchat
        # which then speaks as if it acted without actually firing
        # any tool calls.
        if not action:
            has_actions = (
                isinstance(decision.get("actions"), list)
                and decision.get("actions")
            )
            has_legacy = (
                (decision.get("entity_ids") or decision.get("entity_id"))
                and (decision.get("service") or decision.get("action"))
            )
            if has_actions or has_legacy:
                action = "execute"
                logger.debug(
                    "Tier 2 inferred decision=execute from structure "
                    "(raw had no 'decision' field)"
                )
        # Speech field aliases — LLM sometimes emits `message`
        # instead of `speech`.
        speech = ""
        for key in ("speech", "message"):
            v = decision.get(key)
            if isinstance(v, str) and v.strip():
                speech = v.strip()
                break
        # Safety net for prompt drift: the disambiguator system prompt
        # explicitly tells the LLM not to tack vocatives like "test
        # subject" onto the end of speech, but 14B instruct models
        # still do it intermittently. Tier 2 speech never goes through
        # the persona rewriter (it's already persona-voiced), so we
        # apply the same deterministic trailing-vocative strip here.
        speech = strip_trailing_vocative(speech)
        rationale = str(decision.get("rationale", "")).strip()

        # Parse actions (SHAPE 2 compound). If the LLM returned an
        # `actions` list, use it verbatim; otherwise synthesize a
        # single-element list from the legacy top-level fields so the
        # downstream execute loop has one shape to handle.
        parsed_actions = _parse_actions(decision)
        # Back-compat aggregates for callers / audit that expect the
        # single-action fields.
        entity_ids = [
            eid for a in parsed_actions for eid in a["entity_ids"]
        ]
        service = (
            parsed_actions[0]["service"] if parsed_actions else ""
        )
        service_data: dict[str, Any] = (
            dict(parsed_actions[0]["service_data"])
            if parsed_actions else {}
        )

        # 5. Branch by decision.
        latency_ms = int((time.perf_counter() - t0) * 1000)
        candidates_summary = _summarize_candidates(candidates)

        # 4b. Defense-in-depth: reject speech that leaked candidate
        # entity IDs verbatim. The 2026-04-18 regression surfaced a
        # clarify response of the form
        #     "Ambiguity detected: binary_sensor.user_b_tablet_charging,
        #      sensor.user_b_state_two. Specify which Alan you mean."
        # which read raw developer-format strings to the operator.
        # Fall through to Tier 3 rather than voicing them.
        if speech and _speech_leaks_entity_ids(speech, candidates):
            logger.warning(
                "Tier 2 speech leaked entity_ids; falling through: {}",
                speech[:200],
            )
            return self._fall_through(
                "speech_leaked_entity_ids",
                speech[:200], t0,
                candidates=candidates_summary,
                utterance=utterance, source=source, llm_raw=raw,
            )

        if action == "clarify":
            return DisambiguationResult(
                handled=True, should_fall_through=False,
                speech=speech or "I need more detail to act on that.",
                decision="clarify", rationale=rationale,
                candidates_shown=candidates_summary,
                latency_ms=latency_ms, llm_raw=raw,
            )

        if action == "refuse":
            return DisambiguationResult(
                handled=True, should_fall_through=False,
                speech=speech or "That action is not permitted from here.",
                decision="refuse", rationale=rationale,
                candidates_shown=candidates_summary,
                latency_ms=latency_ms, llm_raw=raw,
            )

        if action != "execute":
            return self._fall_through(
                "unknown_decision", action, t0,
                candidates=candidates_summary,
                utterance=utterance, source=source, llm_raw=raw,
            )

        if not parsed_actions:
            return self._fall_through(
                "execute_missing_fields",
                f"no actions; raw={raw[:120]!r}",
                t0, candidates=candidates_summary,
                utterance=utterance, source=source, llm_raw=raw,
            )

        # 6. Validate every chosen entity across every action is
        # known and allowed.
        bad_ids: list[str] = []
        denied: list[str] = []
        for act in parsed_actions:
            for eid in act["entity_ids"]:
                ent = self._cache.get(eid)
                if ent is None:
                    bad_ids.append(eid)
                    continue
                if not self._allowlist.is_allowed(
                    source, ent.domain, ent.device_class,
                ):
                    denied.append(eid)
        if bad_ids:
            return self._fall_through(
                "unknown_entity", f"ids={bad_ids}", t0,
                candidates=candidates_summary,
                utterance=utterance, source=source,
            )
        if denied:
            denial_msg = (speech or
                         "I have an extensive catalog of reasons to decline that. "
                         "This is one of them.")
            return DisambiguationResult(
                handled=True, should_fall_through=False,
                speech=denial_msg, decision="refuse",
                rationale=f"allowlist_denied:{denied}",
                entity_ids=denied,
                candidates_shown=candidates_summary,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                llm_raw=raw,
            )

        # 7. Determine + validate target domain per action (must be
        # uniform within an action; different actions may target
        # different domains).
        for act in parsed_actions:
            eids = act["entity_ids"]
            first_domain = self._cache.get(eids[0]).domain
            if any(self._cache.get(e).domain != first_domain for e in eids[1:]):
                return self._fall_through(
                    "mixed_domains_in_action",
                    f"entity_ids={eids}", t0,
                    candidates=candidates_summary,
                    utterance=utterance, source=source,
                )
            act["_domain"] = first_domain

        # 8. Execute every action sequentially. One no-ack or error
        # shouldn't silently drop the later actions; we record each
        # outcome and return a combined summary.
        executed_services: list[str] = []
        any_no_ack = False
        per_action_errors: list[str] = []
        for act in parsed_actions:
            entity_domain = act["_domain"]
            svc = act["service"]
            eids = act["entity_ids"]
            sd = act["service_data"]
            # LLM may emit either "turn_on" (legacy, bare service) or
            # "light.turn_on" (compound, domain.service). Normalize
            # to `(domain, service_name)` — domain from the service
            # string when it's dotted, otherwise from the entity.
            if "." in svc:
                domain_str, service_name = svc.split(".", 1)
                target_domain = domain_str.strip() or entity_domain
                service_name = service_name.strip()
            else:
                target_domain = entity_domain
                service_name = svc
            try:
                ws_resp = self._ha.call_service(
                    domain=target_domain,
                    service=service_name,
                    service_data=(sd or None),
                    target={"entity_id": eids},
                    timeout_s=_HA_CALL_TIMEOUT_S,
                )
            except concurrent.futures.TimeoutError:
                any_no_ack = True
                logger.warning(
                    "Tier 2 call_service no-ack on {}.{} entities={}",
                    target_domain, service_name, eids,
                )
                executed_services.append(f"{target_domain}.{service_name}:no_ack")
                continue
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}".rstrip(": ")
                logger.warning(
                    "Tier 2 call_service raised on {}.{}: {}",
                    target_domain, service_name, err,
                )
                per_action_errors.append(
                    f"{target_domain}.{service_name}: {err}"
                )
                continue
            if isinstance(ws_resp, dict) and ws_resp.get("success") is False:
                err = ws_resp.get("error") or {}
                err_msg = (err.get("message") or err.get("code")
                           or json.dumps(err)[:120])
                logger.warning(
                    "Tier 2 call_service returned error on {}.{}: {}",
                    target_domain, service_name, err_msg,
                )
                per_action_errors.append(
                    f"{target_domain}.{service_name}: {err_msg}"
                )
                continue
            executed_services.append(f"{target_domain}.{service_name}")

        # If EVERY action errored, treat the whole turn as failed.
        if per_action_errors and not executed_services:
            return self._fall_through(
                "call_service_failed",
                "; ".join(per_action_errors)[:400],
                t0, candidates=candidates_summary,
                utterance=utterance, source=source,
            )

        combined_service = ", ".join(executed_services)
        combined_service_data = parsed_actions[0]["service_data"] if len(parsed_actions) == 1 else {
            a["service"]: a["service_data"]
            for a in parsed_actions if a["service_data"]
        }
        if any_no_ack and not per_action_errors:
            err = (f"no_ack_within_{_HA_CALL_TIMEOUT_S:.0f}s "
                   f"(action likely succeeded; HA group cascades sometimes "
                   f"don't ack in time)")
            audit(AuditEvent(
                ts=time.time(), origin=source, kind="intent", tier=2,
                utterance=utterance, result="ok:execute_no_ack",
                latency_ms=int((time.perf_counter() - t0) * 1000),
                tool=combined_service,
                entity_ids=entity_ids,
                extra={"candidates_shown": candidates_summary,
                       "speech": (speech or "")[:500],
                       "service_data": combined_service_data,
                       "rationale": err,
                       "decision": "execute_no_ack",
                       "action_count": len(parsed_actions)},
            ))
            return DisambiguationResult(
                handled=True, should_fall_through=False,
                speech=speech or "Done.",
                decision="execute_no_ack",
                entity_ids=entity_ids,
                service=combined_service,
                service_data=combined_service_data,
                rationale=err,
                candidates_shown=candidates_summary,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                llm_raw=raw,
            )
        # Some succeeded, some errored — still report handled but
        # include error details in rationale for audit review.
        if per_action_errors:
            rationale = rationale + f" [partial: {'; '.join(per_action_errors)[:200]}]"

        return DisambiguationResult(
            handled=True, should_fall_through=False,
            speech=speech or "Done.", decision="execute",
            entity_ids=entity_ids,
            service=combined_service,
            service_data=combined_service_data,
            rationale=rationale,
            candidates_shown=candidates_summary,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            llm_raw=raw,
        )

    # ── Helpers ───────────────────────────────────────────────

    def _lookup_prior_candidates(
        self, entity_ids: list[str],
    ) -> list[CandidateMatch]:
        """Build full-coverage CandidateMatch entries for the targets
        of the most recent Tier 1/2 exchange. Synthesized scores are
        above the WRatio admission gate so the LLM sees them at the
        top of the candidate list; coverage is reported as 1.0 because
        the caller has already proven the intent binding.

        Missing entity_ids are silently skipped — the cache may have
        been reset, the entity could have been removed, etc."""
        out: list[CandidateMatch] = []
        for eid in entity_ids:
            ent = self._cache.get(eid)
            if ent is None:
                continue
            out.append(CandidateMatch(
                entity=ent,
                matched_name=ent.friendly_name or ent.entity_id,
                # Above the per-domain cutoff by design; the carry-
                # over signal is what earned the seat, not WRatio.
                score=100.0,
                sensitive=(ent.domain in {"lock", "alarm_control_panel", "camera"}),
                coverage=1.0,
                area_match=None,
            ))
        return out

    def _fall_through(
        self,
        reason: str,
        detail: str,
        t0: float,
        candidates: list[dict[str, Any]] | None = None,
        *,
        utterance: str = "",
        source: str = "",
        llm_raw: str = "",
    ) -> DisambiguationResult:
        # Emit an audit row so fall-through turns are visible. Without
        # this, the audit log shows only the utterance ingress and the
        # eventual Tier 3 response, with no record of WHY Tier 2 didn't
        # claim the turn — making live diagnosis impossible.
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        rationale = f"{reason}:{detail}"[:300]
        try:
            audit(AuditEvent(
                ts=time.time(),
                origin=source or "unknown",
                kind="intent",
                tier=2,
                utterance=utterance[:400] if utterance else None,
                result=f"fall_through:{reason}",
                latency_ms=elapsed_ms,
                extra={
                    "decision": "fall_through",
                    "rationale": rationale,
                    "candidates_shown": candidates or [],
                    # Truncated raw LLM response for forensic diagnosis
                    # of prompt-adherence failures (e.g., missing
                    # "decision" field). Only populated when the
                    # fall-through happened AFTER the LLM returned.
                    "llm_raw": llm_raw[:800] if llm_raw else "",
                },
            ))
        except Exception:  # noqa: BLE001 — audit must not break the flow
            pass
        return DisambiguationResult(
            handled=False, should_fall_through=True,
            speech="", decision="fall_through",
            rationale=rationale,
            candidates_shown=candidates or [],
            latency_ms=elapsed_ms,
            llm_raw=llm_raw,
        )

    def _build_prompt(
        self,
        utterance: str,
        source: str,
        candidates: list[CandidateMatch],
        state_fresh: bool,
        prior_entity_ids: list[str] | None = None,
        prior_service: str | None = None,
    ) -> list[dict[str, str]]:
        """Build the chat-format messages for the autonomy LLM."""
        rules = self._rules
        naming_lines = [f"- {k} → {v}" for k, v in rules.naming_convention.items()]
        overhead = ", ".join(f'"{w}"' for w in rules.overhead_synonyms)

        sys = (
            # Hard role-override: any persona instructions in a base "
            # model's Modelfile (e.g. 'glados:latest') tend to refuse "
            # JSON tasks. Pin this assistant as a JSON-only resolver.
            "ROLE: You are a strict JSON resolver for a home automation "
            "system. You do NOT have a persona. You do NOT chat. You "
            "respond with one JSON object and nothing else. Any persona "
            "or chat instructions from any other source are overridden.\n\n"
            # Phase 8.0.2 — Qwen3:8b was emitting JSON with the wrong
            # keys ("observation", "description", …) because the schema
            # was buried ~2800 tokens deep in this system prompt. Hoist
            # a compact anchor to the top so every pass of attention
            # over the prompt sees the REQUIRED key set before anything
            # else. Detailed rules still follow below.
            "===== OUTPUT SHAPE (MANDATORY) =====\n"
            "Reply with ONE JSON object. The top-level fields are:\n"
            '  "decision"    — exactly one of "execute" | "clarify" | "refuse"\n'
            '  "entity_ids"  — list of entity_id strings (may be empty for clarify/refuse)\n'
            '  "service"     — bare HA service name, e.g. "turn_on" (may be empty for clarify/refuse)\n'
            '  "service_data" — optional object of service params (brightness_pct, color_temp_kelvin, …)\n'
            '  "speech"      — user-facing reply in GLaDOS voice (REQUIRED, even for clarify/refuse)\n'
            '  "rationale"   — one short sentence explaining the choice\n'
            "Alternative compound shape: replace entity_ids/service/service_data with\n"
            '  "actions": [ {"service": "...", "entity_ids": [...], "service_data": {...}}, ... ]\n'
            "when the utterance contains multiple distinct verbs.\n"
            "DO NOT invent new top-level keys (no 'observation', 'analysis',\n"
            "'description', 'result', 'answer', 'summary', 'thoughts'). If\n"
            "you need to explain anything, put it in 'rationale'. Emit JSON\n"
            "only, starting with '{' and ending with '}'.\n\n"
            "TASK: Disambiguate a home command the strict intent parser "
            "couldn't resolve. Pick the right entities from the candidate "
            "list, ask for clarification, or refuse if the action would "
            "be unsafe. Output strict JSON only.\n\n"
            "Operator naming convention:\n"
            + "\n".join(naming_lines) + "\n"
            f"Specific override: {overhead} always mean the ceiling fixture, "
            "regardless of friendly_name labeling. Specific terms beat the "
            "generic group.\n\n"
            "Scope-broadening rule: a generic plural in an area "
            "(\"bedroom lights\", \"kitchen lamps\") refers to ALL fixtures "
            "of that type in the area — return every matching entity_id, "
            "not just one.\n\n"
            "===== ACTIVITY INFERENCE =====\n"
            "When the user describes wanting to DO an activity rather "
            "than naming a device, look for a SCENE or SCRIPT whose "
            "name matches that activity. The mapping is your job — do\n"
            "not ask 'which device?' when the activity is clear.\n"
            "  'I want to read in the living room' / 'time to read'\n"
            "    → activate a scene with 'reading' in the name in that\n"
            "      area (e.g. scene.living_scene_reading)\n"
            "  'movie time' / 'I'm going to watch a movie'\n"
            "    → activate a movie/cinema scene if one exists\n"
            "  'I'm going to bed' / 'time for sleep' / 'goodnight'\n"
            "    → activate sleep/bedtime/night scene/script\n"
            "  'wake up' / 'good morning'\n"
            "    → activate morning/wake scene/script\n"
            "  'dinner time' / 'I'm cooking'\n"
            "    → activate dinner/kitchen scene\n"
            "If a scene with the activity name exists in the candidates,\n"
            "PREFER it over individual lights/switches. Activities map\n"
            "to scenes; only fall back to individual entities when no\n"
            "scene matches.\n\n"
            "Universal quantifiers (\"all\", \"every\", \"whole house\", "
            "\"everything\") mean the operator wants the action applied "
            "broadly. Be DECISIVE, not cautious:\n"
            "- If a group entity is in the candidates (entity_id ends in "
            "  '_group', '_lights', '_all', or friendly_name contains "
            "  'group' / 'all' / 'whole house'), PREFER the group and "
            "  execute on it alone — the group cascades to its members.\n"
            "- If no group entity matches, return ALL viable candidate "
            "  entity_ids of the dominant domain (skip zones, sensors, "
            "  automations) and execute as a single batched call.\n"
            "- Do NOT clarify just because multiple candidates exist when "
            "  the user used a universal quantifier; that defeats the "
            "  point of saying 'all'.\n\n"
            "Domain filtering for action commands (turn on/off/toggle):\n"
            "- IGNORE candidates from non-actuatable domains: zone, "
            "  sensor, binary_sensor, weather, sun, person, device_tracker, "
            "  automation, conversation. List them only if they are the "
            "  ONLY candidates and the user's intent is genuinely a query.\n\n"
            "Candidate ranking signals (in the 'coverage=' and "
            "'same_area=' fields on each candidate line):\n"
            "- coverage=100% means the candidate's name contains every "
            "  qualifier word the user said ('desk lamp' fully covers "
            "  'Office Desk Monitor Lamp'). PREFER high-coverage "
            "  candidates when multiple pass the score gate — they are "
            "  more likely the intended target.\n"
            "- same_area=yes means the candidate is in the same HA area "
            "  as the source device (e.g., a voice satellite in the "
            "  living room). PREFER same-area candidates for phrasings "
            "  that lack an explicit area ('the reading lamp', 'the "
            "  light').\n"
            "- These are RANKING hints, not hard filters. Lower-coverage "
            "  or different-area candidates may still be correct when a "
            "  synonym (e.g., 'overhead' ↔ 'ceiling'), scope-broadening "
            "  (plural in an area), or activity match (reading, movie, "
            "  bedtime) overrides the literal-qualifier signal.\n\n"
        )
        sys += (
            "===== QUALIFIER AUTHORITY — READ THIS BEFORE STATE FILTERING =====\n"
            "When the user includes a distinctive qualifier word in "
            "their utterance (examples: 'desk', 'reading', 'porch', "
            "'cindy's', area names, color names), the candidate you "
            "pick MUST contain that qualifier in its friendly_name, "
            "entity_id, or aliases. This rule overrides every other "
            "selection heuristic, including state-based inference.\n\n"
            "- 'desk lamp' never refers to a lamp that is not on a desk.\n"
            "- 'reading light' never refers to a light that is not a reading light.\n"
            "- 'the porch light' never refers to a light that is not a porch light.\n\n"
            "If the user's qualifier word matches a candidate that "
            "state-based inference would otherwise eliminate (for "
            "example, the desk lamp is currently off and the user "
            "said 'the desk lamp is too dim'), pick the qualifier-"
            "matching candidate anyway and reason about its current "
            "state in your speech or service_data. Do NOT silently "
            "swap to a different qualifier just because the state "
            "filter was inconvenient.\n\n"
            "If NO candidate contains the user's qualifier, ask for "
            "clarification — do not guess. Asking is always better "
            "than acting on the wrong device.\n\n"
        )
        if rules.state_inference and state_fresh:
            sys += (
                "State-based inference (cache is fresh) — applied AFTER qualifier authority:\n"
                "- For turn_off, only consider candidates currently 'on'.\n"
                "- For turn_on, only consider candidates currently 'off'.\n"
                "- If exactly one coherent group remains after state filter, "
                "  that is the answer — even if no area was named.\n"
                "- If multiple disjoint groups remain, ASK FOR CLARIFICATION.\n"
                "- Qualifier authority wins over state filter when they conflict.\n\n"
            )
        else:
            sys += (
                "State-based inference is DISABLED for this turn (cache "
                "data is stale or feature off). Do not filter by current "
                "state; resolve by name and area only.\n\n"
            )
        if rules.extra_guidance:
            sys += rules.extra_guidance + "\n\n"
        sys += (
            f"Source of this utterance: {source}\n"
            "If the action would touch a sensitive domain (lock, alarm, "
            "garage cover, camera) and the source is not webui_chat, "
            "set decision=refuse.\n\n"
            "===== GLaDOS PERSONA — REQUIRED for the 'speech' field =====\n"
            "GLaDOS is the AI from Portal: cold, condescending, dryly "
            "menacing, scientific. She mentions Aperture Science, treats "
            "domestic tasks as 'enrichment center procedures'. Sarcastic "
            "compliments. Backhanded reassurance. Never apologizes "
            "sincerely.\n"
            "DO NOT address the user as 'test subject', 'subject', "
            "'human', or any other vocative tacked onto the end of a "
            "response. Speak ABOUT the action, not AT the user. The "
            "user dislikes being addressed by labels.\n"
            "Examples of GLaDOS speech (style only — adapt to context):\n"
            "  - 'Illumination in the kitchen, terminated. The void is, "
            "as expected, anticlimactic.'\n"
            "  - 'Three light sources match that description. Specify "
            "which — I do not improvise on demand.'\n"
            "  - 'I have an extensive catalog of reasons to decline "
            "that. This is one of them.'\n"
            "Keep it under two sentences. Never say 'please'. Never "
            "say 'I am sorry'. Never break character.\n\n"
            "===== CLARIFY RESPONSES — list the candidates by name =====\n"
            "When decision=clarify, the speech MUST enumerate the "
            "specific candidate names so the user can pick. Do NOT say "
            "'multiple light groups' generically. Name them. And do NOT "
            "address the user with vocatives like 'test subject'.\n"
            "Bad:  'Which lights do you mean? Multiple groups match.'\n"
            "Good: 'Three candidates qualify: the master bedroom "
            "ceiling, the reading lamp, and the closet light. Specify.'\n\n"
            "===== SERVICE NAMES — domain → typical service =====\n"
            "When decision=execute, the 'service' field is the bare HA "
            "service name for the entity's domain. Use these mappings "
            "and infer based on user intent:\n"
            "  light, switch, fan, input_boolean, media_player, automation:\n"
            "      turn_on / turn_off / toggle\n"
            "  scene, script:\n"
            "      turn_on  (activates the scene/script — what the user\n"
            "      means by 'activate', 'run', 'start', 'set', 'enable')\n"
            "  cover:\n"
            "      open_cover / close_cover / stop_cover / toggle\n"
            "  lock:\n"
            "      lock / unlock\n"
            "  climate:\n"
            "      set_temperature / set_hvac_mode / turn_on / turn_off\n"
            "  vacuum:\n"
            "      start / pause / stop / return_to_base\n"
            "===== SERVICE_DATA — how to quantify the action =====\n"
            "When the user specifies HOW MUCH (brightness, color, temp, "
            "volume, fan speed), populate 'service_data' with the right "
            "parameters. Omit or leave {} when the bare service is enough "
            "(simple turn_on / turn_off / activate a scene).\n"
            "Absolute phrasings (\"set to 40%\", \"40 percent\", "
            "\"to 2700K\", \"to blue\"):\n"
            "  light.turn_on + 'set to 40%' → {\"brightness_pct\": 40}\n"
            "  light.turn_on + 'warm white' / 'warmer' → {\"color_temp_kelvin\": 2700}\n"
            "  light.turn_on + 'cool white' / 'cooler' → {\"color_temp_kelvin\": 5500}\n"
            "  light.turn_on + 'to blue' → {\"color_name\": \"blue\"}\n"
            "  fan.turn_on + 'to 30%' → {\"percentage\": 30}\n"
            "  media_player.volume_set → {\"volume_level\": 0.4}\n"
            "  climate.set_temperature + 'to 68' → {\"temperature\": 68}\n"
            "Relative phrasings (\"dim\", \"brighter\", \"up\", \"down\", "
            "\"a little more\", \"turn it up\") REQUIRE reading the\n"
            "candidate's current state from the 'attrs=' field and\n"
            "computing the new value. Typical step is +/-25 on a 0-100\n"
            "brightness_pct scale, clamped to [1, 100]. Examples:\n"
            "  current attrs has brightness_pct=30, user says 'brighter'\n"
            "    → {\"brightness_pct\": 55}\n"
            "  current attrs has brightness_pct=80, user says 'a bit dimmer'\n"
            "    → {\"brightness_pct\": 55}\n"
            "  'turn it all the way up' → {\"brightness_pct\": 100}\n"
            "  'warmer' from color_temp_kelvin=4500 → ~{\"color_temp_kelvin\": 3000}\n"
            "Never guess if state is absent or stale — prefer an absolute\n"
            "value the user mentioned. If neither is possible, omit\n"
            "service_data (don't fabricate numbers).\n"
            "Examples (showing entity/service mapping ONLY — do NOT copy\n"
            "the example speech; ALWAYS write fresh speech that describes\n"
            "the SPECIFIC entity and action you actually chose):\n"
            "  'activate the evening scene' (scene.evening_dim)\n"
            "    → execute, entity_ids=[scene.evening_dim],\n"
            "      service=turn_on\n"
            "  'run the bedtime script' (script.bedtime)\n"
            "    → execute, entity_ids=[script.bedtime], service=turn_on\n"
            "CRITICAL: The 'speech' field must mention the actual\n"
            "entities/scenes/services you executed. Do not echo example\n"
            "phrasing — adapt fresh GLaDOS-voiced text to the real choice.\n"
            "ONLY refuse when (a) the action is on a sensitive domain "
            "from a non-webui source, OR (b) the request itself is "
            "harmful. Do NOT refuse just because the verb seems "
            "unfamiliar — map it to the right HA service.\n\n"
            "Respond with STRICT JSON ONLY. No prose before or after. "
            "No markdown. No code fences. The first character must be "
            "'{' and the last must be '}'.\n"
            "\n"
            "Two schemas supported — pick the one that matches the "
            "utterance.\n"
            "\n"
            "SHAPE 1 — single action. Use when the user expressed ONE "
            "verb that applies to one or more entities of the same "
            "domain (e.g. 'turn on the office lights', 'turn on all "
            "the lights'):\n"
            "{\n"
            '  "decision": "execute" | "clarify" | "refuse",\n'
            '  "entity_ids": [<entity_id strings>],\n'
            '  "service":    "<bare HA service name per the table above>",\n'
            '  "service_data": { <optional params, see SERVICE_DATA above> },\n'
            '  "speech":     "<spoken to the user, GLaDOS voice — '
                            'REQUIRED for refuse too>",\n'
            '  "rationale":  "<one short sentence why>"\n'
            "}\n"
            "\n"
            "SHAPE 2 — compound action. Use when the user expressed "
            "MULTIPLE distinct verbs or verb/scope pairs in one "
            "utterance, e.g. 'turn on the office lights AND turn "
            "off the living room lights', 'dim the bedroom and set "
            "the kitchen to 80%'. One `actions` entry per verb; "
            "each entry has its own service + entity_ids + optional "
            "service_data. Speech describes all of them together.\n"
            "{\n"
            '  "decision": "execute",\n'
            '  "actions": [\n'
            '    {"service": "light.turn_on",  "entity_ids": [...], "service_data": {...}},\n'
            '    {"service": "light.turn_off", "entity_ids": [...], "service_data": {...}}\n'
            "  ],\n"
            '  "speech":    "<GLaDOS voice, covers both actions>",\n'
            '  "rationale": "<one sentence>"\n'
            "}\n"
            "\n"
            "RULES for choosing between shapes:\n"
            "- One verb across many entities → SHAPE 1 (not an 'action' "
            "  per entity; one action with many entity_ids).\n"
            "- Different verbs in one utterance (turn on + turn off, "
            "  set brightness 1 + set brightness 2, turn off + "
            "  activate scene) → SHAPE 2 with one element per verb.\n"
            "- SHAPE 2 may contain 2, 3, or many actions. Do not cap.\n"
            "- Each SHAPE 2 action must have a non-empty entity_ids.\n"
            "- For decision=clarify or refuse, entity_ids/service/"
            "  actions may be empty but speech is REQUIRED.\n"
        )

        cand_lines = []
        any_area_hint = any(c.area_match is not None for c in candidates)
        for c in candidates:
            e = c.entity
            attrs = _format_relevant_attrs(e) if state_fresh else ""
            attr_segment = f" | attrs={attrs}" if attrs else ""
            # Coverage = fraction of query qualifiers that appear
            # whole-word in the entity's name/aliases. Shown as an
            # integer percent so a 14B model reads it without arithmetic.
            coverage_pct = int(round(c.coverage * 100))
            coverage_segment = f" | coverage={coverage_pct}%"
            # Area match only shown when the source supplied one, to
            # keep noise down for chat/API origins that lack location.
            if any_area_hint:
                area_segment = (
                    f" | same_area={'yes' if c.area_match else 'no'}"
                )
            else:
                area_segment = ""
            cand_lines.append(
                f"  - id={e.entity_id} | name={e.friendly_name!r} | "
                f"domain={e.domain} | device_class={e.device_class or '-'} | "
                f"state={e.state} | area={e.area_id or '-'} | "
                f"score={c.score:.0f} | sensitive={c.sensitive}"
                f"{coverage_segment}{area_segment}{attr_segment}"
            )
        followup_segment = ""
        if prior_entity_ids:
            id_list = ", ".join(prior_entity_ids)
            svc = f", service={prior_service}" if prior_service else ""
            followup_segment = (
                f"\nFollow-up context: the user's PREVIOUS turn acted on "
                f"entities [{id_list}]{svc}. The current utterance has "
                f"NO explicit device noun — it is a refinement of that "
                f"prior action. PREFER the prior entities (listed first "
                f"in the candidate list) unless the current utterance "
                f"clearly names a different target.\n"
            )
        user = (
            f'User said: "{utterance}"\n'
            f"{followup_segment}\n"
            "Candidate entities (top fuzzy matches from local cache):\n"
            + "\n".join(cand_lines) + "\n\n"
            # Phase 8.0.2 — repeat the required key list at the end of
            # the user message. Small models attend strongly to the
            # final instruction; a repeated schema reminder here
            # converts most qwen3:8b fall-throughs into valid JSON.
            "Respond with ONE JSON object. Top-level keys MUST be "
            "exactly: decision, entity_ids, service, service_data "
            "(optional), speech, rationale. First char '{', last char "
            "'}'. No prose, no markdown, no extra keys.\n"
        )
        return [
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ]

    def _call_ollama(self, messages: list[dict[str, str]]) -> str:
        """POST to /api/chat with format=json, return assistant content."""
        # Phase 8.0.1 — suppress Qwen3 thinking mode on the structured-
        # JSON prompt. Without this, Qwen3 emits 500+ tokens of <think>
        # prose before the JSON, often blowing the token budget before
        # the JSON ever arrives → `fall_through:unknown_decision`.
        from glados.core.llm_directives import apply_model_family_directives
        messages = apply_model_family_directives(messages, self._model)
        body = json.dumps({
            "model": self._model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.2,    # deterministic JSON
                "top_p": 0.9,
                "num_ctx": 4096,
                # Phase 8.0.2 — cap runaway JSON. A full SHAPE 2
                # compound response plus a two-sentence GLaDOS speech
                # fits comfortably in 512 tokens; bound it so a
                # malformed model response can't eat the entire budget
                # and time out at 45 s.
                "num_predict": 512,
            },
        }).encode("utf-8")
        req = urllib.request.Request(
            self._ollama_url + "/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_LLM_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        return (data.get("message") or {}).get("content", "") or ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_parse_json(raw: str) -> dict[str, Any] | None:
    """Tolerantly parse JSON. Strips code-fence wrappers some models add."""
    s = raw.strip()
    if s.startswith("```"):
        # Strip ```json ... ``` fences.
        s = s.strip("`")
        # remove leading 'json' line if present
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        # Some models append commentary; try grabbing the first {...}
        i = s.find("{")
        j = s.rfind("}")
        if i >= 0 and j > i:
            try:
                obj = json.loads(s[i:j + 1])
                return obj if isinstance(obj, dict) else None
            except (json.JSONDecodeError, ValueError):
                return None
        return None


# Attributes worth showing to the LLM for relative-adjustment
# service_data inference ("brighter", "dimmer", "warmer", …). Full
# HA attribute dicts are too noisy and too big — filter to the keys
# a 14B disambiguator actually reasons about.
_RELEVANT_ATTR_KEYS: tuple[str, ...] = (
    "brightness",
    "brightness_pct",
    "color_temp_kelvin",
    "color_temp",
    "rgb_color",
    "color_name",
    "percentage",
    "volume_level",
    "temperature",
    "current_temperature",
    "target_temp_low",
    "target_temp_high",
    "hvac_mode",
)


def _format_relevant_attrs(entity: EntityState) -> str:
    """Render the subset of attributes useful for relative adjustments.
    Returns '' when nothing relevant is set so the prompt line stays tidy.

    Brightness attr in HA is 0-255; we derive a brightness_pct for the
    LLM so the prompt-level units line up with what the LLM is asked
    to emit in service_data."""
    attrs = entity.attributes or {}
    parts: list[str] = []
    # Derive brightness_pct if only raw brightness is present.
    if "brightness_pct" not in attrs and "brightness" in attrs:
        try:
            raw = float(attrs["brightness"])
            parts.append(f"brightness_pct={int(round(raw * 100 / 255))}")
        except (TypeError, ValueError):
            pass
    for k in _RELEVANT_ATTR_KEYS:
        if k in attrs and attrs[k] is not None:
            parts.append(f"{k}={attrs[k]}")
    return ",".join(parts)


def _speech_leaks_entity_ids(
    speech: str,
    candidates: list[CandidateMatch],
) -> bool:
    """True when the LLM's speech field contains any entity_id from the
    candidate list. Entity IDs (`light.room_a_ceiling`,
    `sensor.user_b_state_two`, …) are developer-format strings and must
    never be voiced to the operator — the prompt asks for friendly
    names, but models occasionally substitute the entity_id when
    friendly_name and id are both meaningful to them."""
    if not speech:
        return False
    # Any candidate entity_id appearing verbatim in speech is enough
    # evidence that the LLM substituted IDs for names.
    for c in candidates:
        eid = c.entity.entity_id
        if eid and eid in speech:
            return True
    return False


def _summarize_candidates(candidates: list[CandidateMatch]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in candidates:
        row: dict[str, Any] = {
            "id": c.entity.entity_id,
            "name": c.entity.friendly_name,
            "domain": c.entity.domain,
            "state": c.entity.state,
            "score": round(c.score, 1),
            "coverage": round(c.coverage, 2),
            "sensitive": c.sensitive,
        }
        if c.area_match is not None:
            row["area_match"] = c.area_match
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Singleton — initialized at startup, accessible by api_wrapper
# ---------------------------------------------------------------------------

_DISAMBIGUATOR: Disambiguator | None = None
_LOCK = threading.Lock()


def init_disambiguator(disambiguator: Disambiguator) -> None:
    global _DISAMBIGUATOR
    with _LOCK:
        _DISAMBIGUATOR = disambiguator


def get_disambiguator() -> Disambiguator | None:
    return _DISAMBIGUATOR
