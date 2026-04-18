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

import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from glados.ha import HAClient, get_cache, get_client
from glados.ha.entity_cache import CandidateMatch, EntityCache, EntityState

from .rules import (
    DisambiguationRules,
    IntentAllowlist,
    domain_filter_for_utterance,
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

# Ollama call timeout. Disambiguator is on the latency-sensitive path
# but the LLM still needs time to produce JSON; 8s gives the autonomy
# model plenty of room without blocking the user excessively.
_LLM_TIMEOUT_S = 8.0
_HA_CALL_TIMEOUT_S = 5.0


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

        # 1. Pull candidates from the cache.
        domain_hint = domain_filter_for_utterance(utterance)
        candidates = self._cache.get_candidates(
            utterance,
            domain_filter=domain_hint,
            limit=self._rules.candidate_limit,
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
            self._ha.call_service(
                domain=target_domain,
                service=service,
                target={"entity_id": entity_ids},
                timeout_s=_HA_CALL_TIMEOUT_S,
            )
        except Exception as exc:
            return self._fall_through(
                "call_service_failed", str(exc), t0,
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
            "You disambiguate ambiguous home automation commands. The user "
            "said something the strict intent parser couldn't resolve. Pick "
            "the right action from a candidate list, ask for clarification, "
            "or refuse if the action would be unsafe.\n\n"
            "Operator naming convention:\n"
            + "\n".join(naming_lines) + "\n"
            f"Specific override: {overhead} always mean the ceiling fixture, "
            "regardless of friendly_name labeling. Specific terms beat the "
            "generic group.\n\n"
            "Scope-broadening rule: a generic plural in an area "
            "(\"bedroom lights\", \"kitchen lamps\") refers to ALL fixtures "
            "of that type in the area — return every matching entity_id, "
            "not just one.\n\n"
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
            "REFUSE with a persona-voiced denial.\n\n"
            "Respond with strict JSON, no extra text:\n"
            "{\n"
            '  "decision": "execute" | "clarify" | "refuse",\n'
            '  "entity_ids": [<entity_id strings>],   // empty for clarify/refuse\n'
            '  "service":    "turn_on" | "turn_off" | "toggle" | "open_cover" | ...,\n'
            '  "speech":     "<what the assistant says, in GLaDOS voice>",\n'
            '  "rationale":  "<one sentence why>"\n'
            "}\n"
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
