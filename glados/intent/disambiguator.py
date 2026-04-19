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
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from glados.observability import AuditEvent, audit

from loguru import logger

from glados.ha import HAClient, get_cache, get_client
from glados.ha.entity_cache import CandidateMatch, EntityCache, EntityState

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

    # ── Entry point ───────────────────────────────────────────

    def run(self, utterance: str, source: str) -> DisambiguationResult:
        """Drive a single utterance through Tier 2."""
        t0 = time.perf_counter()

        # 0. Home-command precheck. Without this, a conversational
        # utterance like "Say hello to my friend, his name is Alan"
        # gets fuzzy-matched against every entity in the house and
        # the LLM produces an ambiguity response. Tier 3 is the
        # right place to handle chitchat.
        if not looks_like_home_command(utterance):
            return self._fall_through(
                "no_home_command_intent",
                utterance[:120], t0,
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
        )
        if not candidates:
            return self._fall_through(
                "no_candidates",
                f"no entities matched query={utterance!r} domains={domain_hint}",
                t0,
            )

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
        )
        try:
            raw = self._call_ollama(prompt_messages)
        except Exception as exc:
            return self._fall_through(
                "llm_call_failed", str(exc), t0,
                candidates=_summarize_candidates(candidates),
            )

        # 4. Parse the JSON response.
        decision = _safe_parse_json(raw)
        if decision is None:
            return self._fall_through(
                "llm_bad_json", raw[:200], t0,
                candidates=_summarize_candidates(candidates),
            )

        action = str(decision.get("decision", "")).lower()
        speech = str(decision.get("speech", "")).strip()
        rationale = str(decision.get("rationale", "")).strip()
        entity_ids = [str(e) for e in (decision.get("entity_ids") or [])
                      if isinstance(e, (str,))]
        service = str(decision.get("service", "")).strip()

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
            )

        if not entity_ids or not service:
            return self._fall_through(
                "execute_missing_fields",
                f"entity_ids={entity_ids} service={service}",
                t0, candidates=candidates_summary,
            )

        # 6. Validate every chosen entity is known and allowed.
        bad_ids: list[str] = []
        denied: list[str] = []
        for eid in entity_ids:
            ent = self._cache.get(eid)
            if ent is None:
                bad_ids.append(eid)
                continue
            if not self._allowlist.is_allowed(source, ent.domain, ent.device_class):
                denied.append(eid)
        if bad_ids:
            return self._fall_through(
                "unknown_entity", f"ids={bad_ids}", t0,
                candidates=candidates_summary,
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

        # 7. Determine target domain (must be uniform; reject mixed).
        target_domain = self._cache.get(entity_ids[0]).domain
        if any(self._cache.get(e).domain != target_domain for e in entity_ids[1:]):
            return self._fall_through(
                "mixed_domains",
                f"entity_ids={entity_ids}", t0,
                candidates=candidates_summary,
            )

        # 8. Execute via WS call_service.
        try:
            ws_resp = self._ha.call_service(
                domain=target_domain,
                service=service,
                target={"entity_id": entity_ids},
                timeout_s=_HA_CALL_TIMEOUT_S,
            )
        except concurrent.futures.TimeoutError:
            # HA didn't ack within the window. The action is usually
            # already in flight (HA's WS acks acceptance, not
            # completion) — turning off a group that cascades to many
            # entities is the common case. We can't promise success,
            # but we shouldn't claim failure either. Audit clearly,
            # then optimistically return success speech.
            err = (f"no_ack_within_{_HA_CALL_TIMEOUT_S:.0f}s "
                   f"(action likely succeeded; HA group cascades sometimes "
                   f"don't ack in time)")
            logger.warning("Tier 2 call_service no-ack on {}.{} entities={}",
                           target_domain, service, entity_ids)
            audit(AuditEvent(
                ts=time.time(), origin=source, kind="intent", tier=2,
                utterance=utterance, result="ok:execute_no_ack",
                latency_ms=int((time.perf_counter() - t0) * 1000),
                tool=f"{target_domain}.{service}",
                entity_ids=entity_ids,
                rationale=err,
                extra={"candidates_shown": candidates_summary,
                       "speech": (speech or "")[:500],
                       "decision": "execute_no_ack"},
            ))
            return DisambiguationResult(
                handled=True, should_fall_through=False,
                speech=speech or "Done.",
                decision="execute_no_ack",
                entity_ids=entity_ids,
                service=f"{target_domain}.{service}",
                rationale=err,
                candidates_shown=candidates_summary,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                llm_raw=raw,
            )
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}".rstrip(": ")
            logger.warning("Tier 2 call_service raised: {}", err)
            return self._fall_through(
                "call_service_failed", err, t0,
                candidates=candidates_summary,
            )
        # HA's WS may return success=false with an error payload instead
        # of raising. Inspect and treat that as a fall-through too.
        if isinstance(ws_resp, dict) and ws_resp.get("success") is False:
            err = ws_resp.get("error") or {}
            err_msg = (err.get("message") or err.get("code")
                       or json.dumps(err)[:120])
            logger.warning("Tier 2 call_service returned error: {}", err_msg)
            return self._fall_through(
                "call_service_returned_error", err_msg, t0,
                candidates=candidates_summary,
            )

        return DisambiguationResult(
            handled=True, should_fall_through=False,
            speech=speech or "Done.", decision="execute",
            entity_ids=entity_ids, service=f"{target_domain}.{service}",
            rationale=rationale,
            candidates_shown=candidates_summary,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            llm_raw=raw,
        )

    # ── Helpers ───────────────────────────────────────────────

    def _fall_through(
        self,
        reason: str,
        detail: str,
        t0: float,
        candidates: list[dict[str, Any]] | None = None,
    ) -> DisambiguationResult:
        return DisambiguationResult(
            handled=False, should_fall_through=True,
            speech="", decision="fall_through",
            rationale=f"{reason}:{detail}"[:300],
            candidates_shown=candidates or [],
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )

    def _build_prompt(
        self,
        utterance: str,
        source: str,
        candidates: list[CandidateMatch],
        state_fresh: bool,
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
        )
        if rules.state_inference and state_fresh:
            sys += (
                "State-based inference (cache is fresh):\n"
                "- For turn_off, only consider candidates currently 'on'.\n"
                "- For turn_on, only consider candidates currently 'off'.\n"
                "- If exactly one coherent group remains after state filter, "
                "  that is the answer — even if no area was named.\n"
                "- If multiple disjoint groups remain, ASK FOR CLARIFICATION.\n\n"
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
            "Schema:\n"
            "{\n"
            '  "decision": "execute" | "clarify" | "refuse",\n'
            '  "entity_ids": [<entity_id strings>],\n'
            '  "service":    "<bare HA service name per the table above>",\n'
            '  "speech":     "<spoken to the user, GLaDOS voice — '
                            'REQUIRED for refuse too>",\n'
            '  "rationale":  "<one short sentence why>"\n'
            "}\n"
            "For decision=clarify or refuse, entity_ids and service may be empty, "
            "but speech is REQUIRED and must be in GLaDOS voice.\n"
        )

        cand_lines = []
        for c in candidates:
            e = c.entity
            cand_lines.append(
                f"  - id={e.entity_id} | name={e.friendly_name!r} | "
                f"domain={e.domain} | device_class={e.device_class or '-'} | "
                f"state={e.state} | area={e.area_id or '-'} | "
                f"score={c.score:.0f} | sensitive={c.sensitive}"
            )
        user = (
            f'User said: "{utterance}"\n\n'
            "Candidate entities (top fuzzy matches from local cache):\n"
            + "\n".join(cand_lines) + "\n\n"
            "Decide and respond as JSON."
        )
        return [
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ]

    def _call_ollama(self, messages: list[dict[str, str]]) -> str:
        """POST to /api/chat with format=json, return assistant content."""
        body = json.dumps({
            "model": self._model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.2,    # deterministic JSON
                "top_p": 0.9,
                "num_ctx": 4096,
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
    return [
        {
            "id": c.entity.entity_id,
            "name": c.entity.friendly_name,
            "domain": c.entity.domain,
            "state": c.entity.state,
            "score": round(c.score, 1),
            "sensitive": c.sensitive,
        }
        for c in candidates
    ]


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
