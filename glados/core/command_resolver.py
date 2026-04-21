"""CommandResolver — the one door for home-control intents.

Every chat completion that hits `POST /v1/chat/completions` eventually
passes its utterance + SourceContext through `CommandResolver.resolve()`.
The resolver orchestrates (in this order):

  1. **Session carry-over.** If the session has a recent enough last
     turn, its entity_ids + service become "prior" context fed into
     Tier 2, so follow-ups like "brighter" / "and the office" resolve.

  2. **Learned-context guess.** When there is no session carry-over
     AND no `area_id` on the request, consult the durable learned-
     context store. If a learned row exists AND HA's current state
     plausibly supports the recorded resolution, that row's
     `(entity_ids, service)` becomes the prior context.

  3. **Tier 1 — HA conversation fast path.** Hits HA's WS
     `conversation/process` for the obvious "turn on the kitchen
     light" / "what time is it" cases. Persona-rewrite on success.

  4. **Tier 2 — LLM disambiguator.** Runs if Tier 1 misses. Uses
     the carry-over / learned prior as synthetic candidates. The
     disambiguator itself prechecks via `looks_like_home_command`
     so chitchat doesn't burn 45 s of LLM time.

  5. **Fall-through.** Returns `should_fall_through=True` so the
     caller runs the full Tier 3 agentic loop.

The resolver does NOT parse English. It builds context, calls the
existing bridge / disambiguator / HA cache, and records the outcome
for the next turn. The LLM (or HA's own intent matcher) makes the
actual decisions.

See `CURRENT_STATE.md` for the architectural rationale.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from loguru import logger

from glados.core.learned_context import LearnedContextStore, LearnedRow
from glados.core.session_memory import SessionMemory, Turn
from glados.core.source_context import SourceContext
from glados.core.user_preferences import UserPreferences
from glados.observability.audit import AuditEvent, audit, now as audit_now


# ---- Default tuning --------------------------------------------------------

# How long a session's last turn remains eligible as carry-over
# context. Shorter than the 10-min SessionMemory idle TTL on purpose:
# the session is "alive" for 10 min, but "brighter" / "them" only
# sticks to the prior turn for ~2 min — beyond that the ambiguity is
# real and the user should be asked.
DEFAULT_CARRYOVER_WINDOW_S = 120.0

DEFAULT_TIER1_TIMEOUT_S = 5.0


# State-query openers — utterances starting with these words are
# asking for a readout, not requesting an action. Tier 2's
# action-shaped prompt can misclassify them as ambiguity and steal
# the turn from Tier 3 (the full agentic LLM that can actually
# query HA state and answer). Short-circuit them to fall-through
# before Tier 1/2 runs.
#
# "who" is included because the CSV has "Who turned on the lights"
# — a logbook-lookup question that's firmly Tier 3's job.
# "why" covers "why is the kitchen so dark" (implicit complaint);
# we route it to Tier 3 so the LLM can either answer factually or
# turn the lights on via MCP — either is better than Tier 2
# clarify-looping.
_STATE_QUERY_OPENERS = re.compile(
    r"^\s*(what|what's|whats|is|are|was|were|who|whom|whose|why|when|where)\b",
    re.IGNORECASE,
)


def _is_state_query(utterance: str) -> bool:
    """True if the utterance opens with a question-form word that
    almost certainly wants a state readout rather than an action."""
    return bool(_STATE_QUERY_OPENERS.match(utterance or ""))


# ---- Result / action types -------------------------------------------------

@dataclass(frozen=True)
class ResolvedAction:
    """Record of what the resolver actually caused to happen.

    Populated on Tier 1 execute (best-effort — Tier 1 doesn't always
    return entity_ids) and on Tier 2 execute (disambiguator returns
    the full service-call shape).
    """

    service: str | None
    entity_ids: tuple[str, ...]
    service_data: dict[str, Any]
    resolved_area_id: str | None


@dataclass(frozen=True)
class ResolverResult:
    """Outcome of one resolve() call.

    `handled`                 resolver produced a user-facing reply
    `should_fall_through`     caller should run the full Tier 3 agentic loop
    `spoken_response`         persona-voiced text to emit back to the user
    `needs_clarification`     resolver asked a question, waits for next turn
    `tier`                    "1" | "2" | None
    `action`                  what was executed, if anything
    `rationale`               short why-string for audit / debugging
    `latency_ms`              end-to-end latency for this resolve
    `learned_row_id`          if a learned-context row was consulted
    """

    handled: bool
    should_fall_through: bool = False
    spoken_response: str | None = None
    needs_clarification: bool = False
    tier: str | None = None
    action: ResolvedAction | None = None
    rationale: str | None = None
    latency_ms: int = 0
    learned_row_id: int | None = None
    ha_conversation_id: str | None = None
    # Phase 8.4 — post-execute state verification carry-through.
    state_verified: bool | None = None
    state_verification: dict[str, Any] | None = None
    # Phase 8.6 — number of actions the planner executed; exposed so
    # the audit log can record compound plans accurately.
    action_count: int | None = None


# ---- Injected collaborator protocols --------------------------------------
#
# The resolver is orchestration; it talks to existing pieces through
# narrow Protocols so tests can supply fakes without pulling in the
# real HA / Ollama stack. Production code passes the real
# ConversationBridge / Disambiguator / PersonaRewriter — they satisfy
# these Protocols structurally.


class _BridgeLike(Protocol):
    def process(
        self,
        text: str,
        conversation_id: str | None = ...,
        language: str | None = ...,
        timeout_s: float = ...,
    ) -> Any: ...


class _DisambiguatorLike(Protocol):
    def run(
        self,
        utterance: str,
        source: str,
        source_area: str | None = ...,
        assume_home_command: bool = ...,
        prior_entity_ids: list[str] | None = ...,
        prior_service: str | None = ...,
    ) -> Any: ...


class _RewriterLike(Protocol):
    def rewrite(self, plain_text: str, context_hint: str | None = ...) -> str: ...


class _EntityCacheLike(Protocol):
    def snapshot(self) -> list[Any]: ...
    def get(self, entity_id: str) -> Any | None: ...


# ---- HA state validator ---------------------------------------------------

class HAStateValidator:
    """Coarse sanity check that a learned-context guess is plausible
    given HA's current state.

    This is NOT a fine-grained precondition engine. Its only job is to
    reject learned rows that would produce obviously-stale or useless
    actions ("turn off the office lights" when nothing is on in the
    office). The real decision still runs through Tier 2; the
    validator just keeps bad guesses from being nominated.

    Rules (kept small on purpose):
      - `*.turn_off` → at least one entity of that domain in the
        target area must currently be `on`
      - `scene.turn_on` → the scene entity, if recorded with a full
        entity_id, must still exist in the cache
      - Everything else → accept (turn_on and brightness changes are
        safe to attempt even when state would make them no-ops)
    """

    def __init__(self, entity_cache: _EntityCacheLike) -> None:
        self._cache = entity_cache

    def validates(
        self,
        *,
        service: str,
        resolved_area_id: str,
        entity_hint: str | None = None,
    ) -> bool:
        if not service:
            return True
        service = service.lower()

        # Scene recall: if the learned row referenced a concrete scene
        # entity_id, verify the scene still exists.
        if service == "scene.turn_on" and entity_hint:
            return self._cache.get(entity_hint) is not None

        # Off-requests need something to turn off.
        if service.endswith(".turn_off"):
            domain = service.split(".", 1)[0]
            return self._any_entity_on(domain=domain, area_id=resolved_area_id)

        # Everything else — don't block. Tier 2 is the real gate.
        return True

    def _any_entity_on(self, *, domain: str, area_id: str) -> bool:
        try:
            snapshot = self._cache.snapshot()
        except Exception as exc:  # defensive — never let validation crash the resolver
            logger.warning("HAStateValidator: cache snapshot failed: {}", exc)
            return True  # be permissive rather than block all learned rows
        for e in snapshot:
            if getattr(e, "domain", None) != domain:
                continue
            if getattr(e, "area_id", None) != area_id:
                continue
            if str(getattr(e, "state", "")).lower() == "on":
                return True
        return False


# ---- Resolver -------------------------------------------------------------

@dataclass
class _Carryover:
    """Prior entity/service context fed into the disambiguator so
    follow-ups without explicit targets still work."""
    entity_ids: list[str] = field(default_factory=list)
    service: str | None = None
    source: str = ""  # "session" | "learned" — for audit / debug
    learned_row_id: int | None = None


class CommandResolver:
    """Orchestrator for home-control intents.

    Constructed once by the engine at startup, reused per request.
    Thread-safe as long as the collaborators are — which the existing
    bridge/disambiguator/cache already are."""

    def __init__(
        self,
        *,
        bridge: _BridgeLike,
        disambiguator: _DisambiguatorLike,
        rewriter: _RewriterLike | None,
        session_memory: SessionMemory,
        learned_context: LearnedContextStore,
        preferences: UserPreferences,
        state_validator: HAStateValidator,
        carryover_window_s: float = DEFAULT_CARRYOVER_WINDOW_S,
        tier1_timeout_s: float = DEFAULT_TIER1_TIMEOUT_S,
        now_fn: Any = None,
    ) -> None:
        if carryover_window_s <= 0:
            raise ValueError("carryover_window_s must be > 0")
        if tier1_timeout_s <= 0:
            raise ValueError("tier1_timeout_s must be > 0")
        self._bridge = bridge
        self._disambiguator = disambiguator
        self._rewriter = rewriter
        self._session = session_memory
        self._learned = learned_context
        # preferences is threaded through for future resolver-side use
        # (tier filtering, task-area logic) once the CSV harness hooks
        # it up. For this MVP, Tier 2's prompt already uses its own
        # preferences plumbing.
        self._prefs = preferences
        self._validator = state_validator
        self._carryover_window_s = carryover_window_s
        self._tier1_timeout_s = tier1_timeout_s
        self._now = now_fn or time.time

    # ---- Entry point ----------------------------------------------------

    def resolve(self, utterance: str, ctx: SourceContext) -> ResolverResult:
        """Drive a single utterance + context through the resolver.

        Returns a `ResolverResult` telling the caller whether a reply
        was produced, whether Tier 3 should run, and what (if anything)
        was executed.
        """
        started_perf = time.perf_counter()
        utterance = (utterance or "").strip()
        if not utterance:
            # STT artifact or empty submission — do nothing, don't
            # burn Tier 3 on an empty message.
            return ResolverResult(
                handled=True,
                spoken_response=None,
                rationale="empty_utterance",
                latency_ms=self._elapsed_ms(started_perf),
            )

        # State-query short-circuit: utterances opening with a
        # question word ("what lights are on?", "is the office light
        # on?", "who turned on the lights") are asking for a readout.
        # Skip Tier 1/2 and let the Tier 3 agentic loop answer with
        # HA MCP state queries. Prevents the action-shaped Tier 2
        # prompt from misclassifying questions as ambiguity.
        if _is_state_query(utterance):
            return ResolverResult(
                handled=False,
                should_fall_through=True,
                rationale="state_query",
                latency_ms=self._elapsed_ms(started_perf),
            )

        carryover = self._build_carryover(utterance, ctx)

        # Tier 1
        t1 = self._try_tier1(utterance, ctx)
        if t1 is not None and t1.handled:
            self._record_turn(ctx, utterance, t1.action,
                              ha_conversation_id=t1.ha_conversation_id)
            self._audit(ctx, utterance, tier=1, action=t1.action, result="ok",
                        latency_ms=t1.latency_ms, rationale=t1.rationale)
            return t1

        # Tier 2
        t2 = self._try_tier2(utterance, ctx, carryover)
        if t2.handled:
            self._record_turn(ctx, utterance, t2.action,
                              ha_conversation_id=t2.ha_conversation_id)
            if t2.action and not t2.needs_clarification:
                self._reinforce_learned(
                    utterance=utterance, ctx=ctx, action=t2.action,
                    consulted_row_id=carryover.learned_row_id if carryover else None,
                )
            self._audit(ctx, utterance, tier=2, action=t2.action,
                        result="ok" if not t2.needs_clarification else "clarify",
                        latency_ms=t2.latency_ms, rationale=t2.rationale,
                        state_verified=t2.state_verified,
                        state_verification=t2.state_verification,
                        action_count=t2.action_count)
            return t2

        # A learned guess that we nominated but Tier 2 couldn't use is
        # penalized — next time, the resolver shouldn't trust that row
        # as readily. Reinforcement is the only self-correcting signal
        # the store has.
        if carryover and carryover.learned_row_id is not None:
            self._learned.bump_failure(carryover.learned_row_id)

        return ResolverResult(
            handled=False,
            should_fall_through=True,
            rationale=t2.rationale or "tier2_fall_through",
            latency_ms=self._elapsed_ms(started_perf),
            learned_row_id=carryover.learned_row_id if carryover else None,
        )

    # ---- Carryover build -----------------------------------------------

    def _build_carryover(
        self, utterance: str, ctx: SourceContext,
    ) -> _Carryover | None:
        """Assemble prior-context to hand to the disambiguator.

        Source preference:
          1. Session's most recent turn, if within the carry-over window
             AND the current utterance looks anaphoric (no distinctive
             qualifier words of its own).
          2. Learned-context lookup, if no session turn AND no source
             area on the request, AND the HA state validator approves.

        Phase 8.3 fix (2026-04-20): prior behavior blindly attached
        the previous turn's entity_ids whenever the session had a
        recent turn. "Bedroom strip segment 3" said right after
        "turn on the desk lamp" inherited `light.task_lamp_one`
        as prior context and the disambiguator read "segment 3" as a
        brightness modifier on the wrong entity. Carry-over is now
        gated on the current utterance being genuinely anaphoric —
        utterances that name their own target skip it and go through
        the retriever normally.
        """
        last = self._session.last_turn(ctx.session_id)
        if last is not None and self._within_window(last):
            # Carry-over is for utterances with no device noun of
            # their own ("brighter", "turn it up"). If the current
            # utterance has distinctive qualifier words, it is
            # naming its own target and we skip carry-over.
            if not self._looks_anaphoric(utterance):
                return None
            return _Carryover(
                entity_ids=list(last.entities_affected),
                service=last.service,
                source="session",
            )

        # Learned-context lookup only kicks in when there's genuinely
        # no local context. A voice request that already carries an
        # area is better served by the disambiguator's normal
        # candidate path.
        if ctx.area_id:
            return None

        candidates = self._learned.lookup(
            utterance=utterance,
            source_channel=ctx.channel,
            source_area_id=None,
            limit=3,
        )
        for row in candidates:
            if self._validate_learned(row):
                return _Carryover(
                    entity_ids=[],  # learned rows don't pin entity_ids
                    service=row.resolved_verb,
                    source="learned",
                    learned_row_id=row.id,
                )
            # First row that doesn't validate just keeps the loop
            # going; a single stale row shouldn't block a still-good
            # alternative (e.g. bedroom → office for "brighter").
        return None

    def _within_window(self, turn: Turn) -> bool:
        return (self._now() - turn.timestamp) <= self._carryover_window_s

    def _looks_anaphoric(self, utterance: str) -> bool:
        """True when the utterance has no distinctive qualifier words
        of its own — a pure refinement that needs prior context to
        resolve ("brighter", "turn it up", "increase by ten percent").

        Utterances with their own qualifier tokens (e.g. "bedroom
        strip segment 3") name a new target and must NOT inherit
        the previous turn's entity_ids — that was the source of the
        Gate 3 failure where "segment 3" got read as a brightness
        value on the desk lamp from the prior turn.
        """
        # Imported here (not at module top) to avoid bootstrapping
        # disambiguator module state before the resolver loads.
        from glados.intent.disambiguator import _extract_qualifiers
        quals = _extract_qualifiers(utterance)
        return not quals

    def _validate_learned(self, row: LearnedRow) -> bool:
        return self._validator.validates(
            service=row.resolved_verb,
            resolved_area_id=row.resolved_area_id,
        )

    # ---- Tier 1 --------------------------------------------------------

    def _try_tier1(self, utterance: str, ctx: SourceContext) -> ResolverResult | None:
        """Run the HA conversation fast path. Returns None when the
        bridge itself is unavailable (wiring error) — caller proceeds
        to Tier 2.

        Thread HA's own conversation_id back in from the last session
        turn so HA maintains its multi-turn intent state (e.g. "All
        lights" inheriting the prior "turn off" verb)."""
        start = time.perf_counter()
        prior_conv = self._prior_ha_conversation_id(ctx)
        try:
            result = self._bridge.process(
                utterance,
                conversation_id=prior_conv,
                timeout_s=self._tier1_timeout_s,
            )
        except Exception as exc:
            logger.warning("CommandResolver: Tier 1 bridge raised: {}", exc)
            return None

        if not getattr(result, "handled", False):
            # Not our target outcome; leave Tier 2 to it.
            return None

        plain = str(getattr(result, "speech", "") or "")
        spoken = self._persona_rewrite(plain, utterance_hint=utterance)
        ha_conv = getattr(result, "conversation_id", None)
        # Tier 1 may or may not surface entity_ids (depends on HA's
        # response shape). Preserve what's available so next-turn
        # carry-over has something to anchor on.
        entity_ids = tuple(getattr(result, "entity_ids", None) or ())
        action = ResolvedAction(
            service=None,
            entity_ids=entity_ids,
            service_data={},
            resolved_area_id=ctx.area_id,
        )
        return ResolverResult(
            handled=True,
            spoken_response=spoken or plain or None,
            tier="1",
            action=action,
            rationale=f"tier1_{getattr(result, 'response_type', '')}",
            latency_ms=self._elapsed_ms(start),
            ha_conversation_id=ha_conv,
        )

    def _prior_ha_conversation_id(self, ctx: SourceContext) -> str | None:
        """Return the HA conversation_id from the most recent turn in
        this session, if any. Used to maintain HA's own multi-turn
        intent context across utterances."""
        last = self._session.last_turn(ctx.session_id)
        if last is None:
            return None
        return last.ha_conversation_id

    # ---- Tier 2 --------------------------------------------------------

    def _try_tier2(
        self,
        utterance: str,
        ctx: SourceContext,
        carryover: _Carryover | None,
    ) -> ResolverResult:
        """Run the LLM disambiguator. Its own `looks_like_home_command`
        precheck decides whether to go to the model at all."""
        start = time.perf_counter()
        assume_home = carryover is not None
        prior_entities = list(carryover.entity_ids) if carryover else []
        prior_service = carryover.service if carryover else None

        try:
            result = self._disambiguator.run(
                utterance=utterance,
                source=ctx.origin,
                source_area=ctx.area_id,
                assume_home_command=assume_home,
                prior_entity_ids=prior_entities,
                prior_service=prior_service,
            )
        except Exception as exc:
            logger.warning("CommandResolver: Tier 2 disambiguator raised: {}", exc)
            return ResolverResult(
                handled=False,
                should_fall_through=True,
                rationale=f"tier2_error:{type(exc).__name__}",
                latency_ms=self._elapsed_ms(start),
            )

        if not getattr(result, "handled", False):
            return ResolverResult(
                handled=False,
                should_fall_through=True,
                rationale=f"tier2_{getattr(result, 'decision', '') or 'fall_through'}",
                latency_ms=self._elapsed_ms(start),
            )

        decision = str(getattr(result, "decision", ""))
        needs_clarification = decision == "clarify"
        entity_ids = tuple(getattr(result, "entity_ids", []) or ())
        service = str(getattr(result, "service", "") or "") or None
        service_data = dict(getattr(result, "service_data", {}) or {})
        resolved_area = self._infer_resolved_area(ctx, entity_ids)
        action = ResolvedAction(
            service=service,
            entity_ids=entity_ids,
            service_data=service_data,
            resolved_area_id=resolved_area,
        ) if entity_ids or service else None

        return ResolverResult(
            handled=True,
            needs_clarification=needs_clarification,
            spoken_response=str(getattr(result, "speech", "") or "") or None,
            tier="2",
            action=action,
            rationale=str(getattr(result, "rationale", "")) or decision or None,
            latency_ms=self._elapsed_ms(start),
            learned_row_id=carryover.learned_row_id if carryover else None,
            state_verified=getattr(result, "state_verified", None),
            state_verification=getattr(result, "state_verification", None) or None,
            action_count=getattr(result, "action_count", None),
        )

    # ---- Bookkeeping ---------------------------------------------------

    def _record_turn(
        self,
        ctx: SourceContext,
        utterance: str,
        action: ResolvedAction | None,
        *,
        ha_conversation_id: str | None = None,
    ) -> None:
        """Push this turn into SessionMemory so next-turn follow-ups
        have context. Nothing is recorded when there's no action —
        we don't want a query ("is the office light on?") to act as
        carry-over for a subsequent unrelated command."""
        if action is None or (not action.entity_ids and not action.service):
            return
        turn = Turn(
            timestamp=self._now(),
            utterance=utterance,
            resolved_area_id=action.resolved_area_id,
            entities_affected=action.entity_ids,
            action_verb=action.service,
            service=action.service,
            service_data=dict(action.service_data),
            ha_conversation_id=ha_conversation_id,
        )
        self._session.record_turn(ctx.session_id, turn)

    def _reinforce_learned(
        self,
        *,
        utterance: str,
        ctx: SourceContext,
        action: ResolvedAction,
        consulted_row_id: int | None,
    ) -> None:
        """Store a (utterance, source → resolution) record or bump
        an existing one. Only runs for Tier 2 executes that resolved
        to a concrete area — a learned row without an area is
        useless for future validation."""
        if not action.service or not action.resolved_area_id:
            return
        try:
            self._learned.record_success(
                utterance=utterance,
                source_channel=ctx.channel,
                source_area_id=ctx.area_id,
                resolved_area_id=action.resolved_area_id,
                resolved_verb=action.service,
                resolved_tier=None,
            )
        except ValueError:
            # Normalized utterance was empty — already handled by the
            # empty-utterance short-circuit; just skip.
            pass
        # If we consulted a row from a different resolution, the new
        # success will have upserted its own row; we don't penalize
        # the old one here (the user's correction itself is the
        # signal, and Tier 2's different execute will get its own
        # reinforcement).
        _ = consulted_row_id  # intentionally unused for now

    # ---- Helpers -------------------------------------------------------

    def _persona_rewrite(self, plain: str, *, utterance_hint: str) -> str:
        if not plain or self._rewriter is None:
            return plain
        try:
            # PersonaRewriter.rewrite returns a RewriteResult dataclass,
            # not a string. Extracting `.text` here prevents the object
            # from leaking into downstream speech fields (where it
            # caused a JSON-serialization crash on Tier 1 replies —
            # seen live on "turn off the basement lights" 2026-04-21).
            out = self._rewriter.rewrite(plain, context_hint=utterance_hint)
            text = getattr(out, "text", None) if out is not None else None
            if isinstance(text, str):
                return text or plain
            if isinstance(out, str):
                # PersonaRewriterProtocol advertises `str` too (line 172
                # of this file); honour stub implementations.
                return out or plain
            return plain
        except Exception as exc:  # best-effort — persona failure never blocks the reply
            logger.debug("CommandResolver: persona rewrite raised: {}", exc)
            return plain

    def _infer_resolved_area(
        self, ctx: SourceContext, entity_ids: tuple[str, ...],
    ) -> str | None:
        """Use the source area if present; otherwise, if all affected
        entities share one HA area, use that. Otherwise None."""
        if ctx.area_id:
            return ctx.area_id
        if not entity_ids:
            return None
        areas: set[str] = set()
        for eid in entity_ids:
            e = self._validator._cache.get(eid)  # cache access is thread-safe
            if e is None:
                continue
            area = getattr(e, "area_id", None)
            if area:
                areas.add(area)
        return areas.pop() if len(areas) == 1 else None

    def _elapsed_ms(self, start: float) -> int:
        return int((time.perf_counter() - start) * 1000)

    def _audit(
        self,
        ctx: SourceContext,
        utterance: str,
        *,
        tier: int,
        action: ResolvedAction | None,
        result: str,
        latency_ms: int,
        rationale: str | None,
        state_verified: bool | None = None,
        state_verification: dict[str, Any] | None = None,
        action_count: int | None = None,
    ) -> None:
        try:
            extra = ctx.to_audit_fields()
            if rationale:
                extra["rationale"] = rationale
            if state_verified is not None:
                extra["state_verified"] = state_verified
            if state_verification:
                extra["state_verification"] = state_verification
            if action_count is not None:
                extra["action_count"] = action_count
            audit(AuditEvent(
                ts=audit_now(),
                origin=ctx.origin,
                kind="intent",
                principal=ctx.principal,
                utterance=utterance,
                tier=tier,
                tool=(action.service if action else None),
                params=(dict(action.service_data) if action else None),
                entity_ids=(list(action.entity_ids) if action else None),
                result=result,
                latency_ms=latency_ms,
                extra=extra,
            ))
        except Exception as exc:  # audit is never allowed to break the resolver
            logger.debug("CommandResolver: audit emit failed: {}", exc)


# ---------------------------------------------------------------------------
# Process-wide singleton (mirrors get_bridge / get_disambiguator /
# get_rewriter in the other Stage 3 packages)
# ---------------------------------------------------------------------------

_RESOLVER: CommandResolver | None = None


def init_resolver(resolver: CommandResolver) -> None:
    """Register the process-wide CommandResolver. Called once at engine
    startup by `server.py`. Subsequent calls replace the previous one."""
    global _RESOLVER
    _RESOLVER = resolver


def get_resolver() -> CommandResolver | None:
    """Return the process-wide CommandResolver, or None if one hasn't
    been initialized yet (HA WS / Tier 2 stack failed to come up)."""
    return _RESOLVER
