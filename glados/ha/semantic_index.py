"""Semantic retrieval over HA entities — Phase 8.3.

BGE-small-en-v1.5 embeddings computed on the container's CPU, then
kept in a compact in-memory matrix keyed on entity_id. The index is
built from EntityCache + HA registries (area, device) on startup
and on every cache resync; persisted to disk so warm restarts skip
the ~2 s embed step.

The disambiguator (and MCP `search_entities` tool in §8.3.4) uses
`retrieve(utterance, k)` instead of the legacy fuzzy top-K. The
raw cosine top-N goes through a device-diversity filter (§8.3.3)
before reaching the planner — this is the non-negotiable gate the
operator flagged for the Gledopto LED-strip pattern.

All embeddings are L2-normalized, so similarity = dot product.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

try:  # hard deps at runtime, but unit tests still need to import.
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]
    _NUMPY_AVAILABLE = False

try:
    import onnxruntime as ort  # type: ignore[import-not-found]
    _ORT_AVAILABLE = True
except ImportError:  # pragma: no cover
    ort = None  # type: ignore[assignment]
    _ORT_AVAILABLE = False

try:
    from tokenizers import Tokenizer  # type: ignore[import-not-found]
    _TOKENIZERS_AVAILABLE = True
except ImportError:  # pragma: no cover
    Tokenizer = None  # type: ignore[assignment]
    _TOKENIZERS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# BGE-small-en-v1.5 metadata. The ONNX export produces 384-d pooled
# embeddings and accepts up to 512 tokens. Entity docs never approach
# that limit — median doc is ~15 tokens — but the tokenizer's
# enable_truncation still kicks in defensively.
_EMBED_DIM: int = 384
_MAX_SEQ_LEN: int = 512

DEFAULT_MODEL_PATH = "/app/models/bge-small-en-v1.5.onnx"
DEFAULT_TOKENIZER_PATH = "/app/models/bge-small-en-v1.5.tokenizer.json"
DEFAULT_INDEX_PATH = "/app/data/entity_embeddings.npz"

# BGE-small expects a retrieval-style query to be prefixed with this
# instruction per the model card. Applying it to queries but NOT
# documents is the recommended pattern — mixing up the sides halves
# the NDCG on MTEB. We keep both formats explicit so the contract is
# auditable.
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


# ---------------------------------------------------------------------------
# Device-diversity filter (Phase 8.3.3 — the four operator-named gates)
#
# The operator's non-negotiable requirement: no matter how well the
# semantic ranker likes every segment of one multi-entity device, the
# top-K that reaches the planner must not be dominated by that one
# device. Gledopto LED strips, WLED zones, Hue gradient strips, and
# multi-head Z-Wave RGB controllers all expose N light.* entities
# sharing one device_id — left unfiltered they swamp top-8 and the
# real "desk lamp" falls off the list.
# ---------------------------------------------------------------------------

# Default token set that marks a segment entity. Matched as whole-
# word substrings (case-insensitive) in the entity's friendly_name
# or in the tail of its entity_id. Operator edits this via the
# existing Integrations → Home Assistant → Disambiguation rules
# card (new "Segment tokens" sub-list added in §8.3.5).
DEFAULT_SEGMENT_TOKENS: tuple[str, ...] = (
    "seg", "segment", "zone", "channel", "strip", "group", "head",
)

# Hard per-device cap in any returned top-K. When the utterance
# doesn't explicitly name a segment, NO device gets more than this
# many entities in the result — even if the raw cosine ranker loved
# N-1 of them. Tuned to 2 per plan spec: allows a light + its paired
# switch companion on the rare case where dedup doesn't apply, but
# rules out "8 bedroom strip segments" dominance.
PER_DEVICE_CAP: int = 2


def _compile_segment_regex(tokens: tuple[str, ...]) -> re.Pattern[str]:
    """Build a regex that matches any of `tokens` followed by a
    NUMBER (e.g. `seg3`, `segment 12`, `zone_4`, `strip 1`). A
    trailing `_<digits>` anywhere in a name also counts — Gledopto
    entity_ids sometimes arrive as `light.room_a_strip_1`.

    Segment-ness requires digits. "bedroom strip" on its own is
    the master entity (the one the operator addresses) and must
    NOT match; "bedroom strip 1" IS a segment and matches.
    Revised 2026-04-20 after a test caught the master-matching
    bug where `light.room_a_strip` was misclassified as a
    segment because "strip" is also a token."""
    if not tokens:
        # No tokens means the filter falls back to the `_<digits>`
        # suffix detector only.
        return re.compile(r"_(\d+)\b", re.IGNORECASE)
    escaped = "|".join(re.escape(t) for t in tokens if t)
    # Token followed by one or more digits (optionally with
    # separator), OR _<digits> anywhere as a suffix marker. The
    # `\d+` (not `\d*`) is the important change — the bare token
    # without a number is the master entity.
    return re.compile(
        rf"\b(?:{escaped})[\s_-]*\d+\b|_\d+\b",
        re.IGNORECASE,
    )


_NUM_RE = re.compile(r"(\d+)")


def _natural_sort_key(name: str) -> tuple[Any, ...]:
    """Sort `Seg 2` before `Seg 10`. Splits on digit runs and
    compares numeric chunks as int."""
    parts = _NUM_RE.split(name or "")
    return tuple(
        int(p) if p.isdigit() else p.lower()
        for p in parts if p
    )


def _entity_is_segment(
    entity_id: str,
    friendly_name: str,
    pattern: re.Pattern[str],
) -> bool:
    """True when the entity's name or id matches the segment pattern.
    Used by `_pick_representative` to prefer non-segment entities and
    by `_utterance_segment_match` to detect operator-intent overrides."""
    if pattern.search(friendly_name or ""):
        return True
    tail = entity_id.split(".", 1)[-1]
    if pattern.search(tail):
        return True
    return False


def _utterance_pin_candidates(
    utterance: str,
    hits: list["SemanticHit"],
    pattern: re.Pattern[str],
) -> set[str]:
    """Return the entity_ids in `hits` that the operator has
    explicitly named via a segment qualifier in the utterance.

    Example: utterance "bedroom strip segment 3 red" with hits for
    `bedroom_strip_seg_3` and `bedroom_strip_seg_4` → pin seg_3.
    When the utterance doesn't mention any segment token at all,
    the set is empty and every group gets normal collapse.
    """
    matches = pattern.findall(utterance or "")
    if not matches:
        return set()
    # Extract the digit parts — we want to pin entities whose name
    # contains the same digit qualifier. "segment 3" and "seg 3"
    # both pin any candidate with "3" adjacent to a segment token.
    digits: set[str] = set()
    for m in _NUM_RE.findall(utterance or ""):
        digits.add(m)
    pinned: set[str] = set()
    for h in hits:
        name = (h.document or "").lower()
        eid_tail = h.entity_id.split(".", 1)[-1].lower()
        # Require BOTH a segment token AND one of the utterance's
        # digits to appear in the entity's name/id.
        if not pattern.search(name) and not pattern.search(eid_tail):
            continue
        if not digits:
            # Utterance had a segment token with no digit (e.g.
            # "the strip") — any segment entity of that device
            # doesn't get pinned; it goes through normal collapse.
            continue
        hit_digits = set(_NUM_RE.findall(name)) | set(
            _NUM_RE.findall(eid_tail)
        )
        if hit_digits & digits:
            pinned.add(h.entity_id)
    return pinned


def _pick_representative(
    group: list["SemanticHit"],
    pattern: re.Pattern[str],
    cache: Any = None,
) -> "SemanticHit":
    """Pick one entity from a same-device group when the utterance
    has no explicit pin. Preference order, matching the plan spec:

      1. Non-segment name wins (the "master" rather than a numbered
         sibling).
      2. `light.*` beats `switch.*` — same rule Phase 8.1 uses for
         twin dedup, hoisted here so the semantic path doesn't need
         a separate dedup pass.
      3. If both are lights, the one with real dim capability
         (`supported_color_modes` richer than just 'onoff') wins —
         Inovelli fan/light edge case.
      4. Highest raw cosine score (keep the model's best pick).
      5. Natural-sort-first by name (deterministic tiebreaker so
         repeat queries return stable results).
    """
    def _is_seg(h: "SemanticHit") -> bool:
        name = (h.document or "")
        return _entity_is_segment(h.entity_id, name, pattern)

    def _domain(h: "SemanticHit") -> str:
        return h.entity_id.split(".", 1)[0]

    def _light_has_dim(h: "SemanticHit") -> bool:
        if _domain(h) != "light" or cache is None:
            return False
        try:
            e = cache.get(h.entity_id)
        except Exception:
            return False
        if e is None:
            return False
        modes = e.attributes.get("supported_color_modes") or []
        if not isinstance(modes, (list, tuple, set)):
            return False
        return any(str(m).lower() != "onoff" for m in modes)

    def sort_key(h: "SemanticHit") -> tuple[Any, ...]:
        # Lower = better. Each component reverses boolean preferences
        # so True (preferred) sorts before False.
        return (
            _is_seg(h),                        # non-seg first
            _domain(h) != "light",             # light before switch
            not _light_has_dim(h),             # dim-capable first
            -h.score,                          # higher score first
            _natural_sort_key(h.document or h.entity_id),
        )

    return sorted(group, key=sort_key)[0]


def apply_device_diversity(
    hits: list["SemanticHit"],
    utterance: str,
    *,
    top_k: int = 8,
    segment_tokens: tuple[str, ...] = DEFAULT_SEGMENT_TOKENS,
    per_device_cap: int = PER_DEVICE_CAP,
    cache: Any = None,
    ignore_segments: bool = True,
) -> list["SemanticHit"]:
    """Filter a raw cosine-ranked hit list to enforce the operator
    contract:

      0. (Default) When `ignore_segments=True`, any entity whose
         name or id matches the segment pattern is dropped entirely
         BEFORE collapse. Operators who only ever address whole
         lamps or scenes get a cleaner top-K; per-segment devices
         fall out of view unless accessed via a scene.
      1. No device_id appears more than `per_device_cap` times in
         the final top-K UNLESS the utterance explicitly pins one
         of its siblings (segment qualifier like "strip 3").
      2. For a same-device group with no pin, keep one
         representative per `_pick_representative`.
      3. Pinned entities bypass the per-device cap — operator
         intent (naming a segment) always wins. Disabled when
         `ignore_segments=True` because pins by definition address
         segments.
      4. Hits without a device_id pass through unchanged; dedup
         requires a registry id to join on.

    Input ranking order is preserved for survivors. The returned
    list is truncated to `top_k`.
    """
    if not hits:
        return []
    pattern = _compile_segment_regex(segment_tokens)
    # Phase 8.3 follow-up — segments are implementation detail in
    # most deployments. Drop them up front so the downstream
    # collapse never wastes effort on entities the operator would
    # never address. When False, fall through to the original
    # pin + collapse behaviour.
    if ignore_segments:
        hits = [
            h for h in hits
            if not _entity_is_segment(
                h.entity_id, h.document or "", pattern,
            )
        ]
        if not hits:
            return []
        pinned_ids: set[str] = set()
    else:
        pinned_ids = _utterance_pin_candidates(utterance, hits, pattern)

    # Group hits by device_id; track per-device kept count.
    by_device: dict[str, list[SemanticHit]] = {}
    loose: list[SemanticHit] = []
    for h in hits:
        if h.device_id:
            by_device.setdefault(h.device_id, []).append(h)
        else:
            loose.append(h)

    # Build the keep-set in original ranking order.
    keep_ids: set[str] = set()
    for did, group in by_device.items():
        # Pinned candidates are always kept regardless of cap.
        for h in group:
            if h.entity_id in pinned_ids:
                keep_ids.add(h.entity_id)
        # Remaining candidates in this group go through collapse.
        non_pinned = [h for h in group if h.entity_id not in pinned_ids]
        if not non_pinned:
            continue
        # When no pin exists and the group has siblings, collapse
        # to a single representative. When a pin DOES exist, we
        # already honored operator intent on that device — drop
        # the rest of the siblings to avoid diluting the top-K.
        if pinned_ids & {h.entity_id for h in group}:
            # Pin exists on this device — siblings get dropped.
            continue
        # No pin → keep one representative.
        rep = _pick_representative(non_pinned, pattern, cache=cache)
        keep_ids.add(rep.entity_id)

    # Assemble the final list by walking the input in its original
    # order so the ranker's ordering is preserved.
    filtered: list[SemanticHit] = []
    per_device_count: dict[str, int] = {}
    for h in hits:
        if h.device_id is None:
            filtered.append(h)
            continue
        if h.entity_id not in keep_ids:
            continue
        # Apply the per-device cap for non-pinned survivors. Pinned
        # entities bypass the cap (operator explicitly named them).
        if h.entity_id in pinned_ids:
            filtered.append(h)
            continue
        current = per_device_count.get(h.device_id, 0)
        if current >= per_device_cap:
            continue
        per_device_count[h.device_id] = current + 1
        filtered.append(h)

    return filtered[:top_k]


def is_semantic_retrieval_available(
    model_path: str | Path = DEFAULT_MODEL_PATH,
    tokenizer_path: str | Path = DEFAULT_TOKENIZER_PATH,
) -> bool:
    """True iff every dependency AND every model file needed to embed
    text is available on this process. Used to gate the
    disambiguator's semantic path with a fuzzy fallback."""
    if not (_NUMPY_AVAILABLE and _ORT_AVAILABLE and _TOKENIZERS_AVAILABLE):
        return False
    return Path(model_path).exists() and Path(tokenizer_path).exists()


# ---------------------------------------------------------------------------
# Embedder — ONNX BGE-small wrapper
# ---------------------------------------------------------------------------

class Embedder:
    """Thin ONNX wrapper.

    Thread-safe: `onnxruntime.InferenceSession.run` releases the GIL
    during native compute, and we batch per-call rather than keeping
    mutable per-instance state outside the session.
    """

    def __init__(
        self,
        model_path: str | Path = DEFAULT_MODEL_PATH,
        tokenizer_path: str | Path = DEFAULT_TOKENIZER_PATH,
    ) -> None:
        if not (_NUMPY_AVAILABLE and _ORT_AVAILABLE and _TOKENIZERS_AVAILABLE):
            raise RuntimeError(
                "Embedder requires numpy + onnxruntime + tokenizers"
            )
        mp = Path(model_path)
        tp = Path(tokenizer_path)
        if not mp.exists():
            raise FileNotFoundError(f"BGE-small ONNX not found: {mp}")
        if not tp.exists():
            raise FileNotFoundError(f"BGE-small tokenizer not found: {tp}")
        # Single-threaded CPU is plenty for ~3500 docs at startup;
        # keep intra/inter low so we don't steal cores from the
        # WebUI + API threads. Operators on beefy hosts can raise.
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(mp), sess_options=opts, providers=["CPUExecutionProvider"],
        )
        self._tokenizer = Tokenizer.from_file(str(tp))
        # BGE encoder expects padded/truncated input with attention
        # masks; let the tokenizer handle both.
        self._tokenizer.enable_truncation(max_length=_MAX_SEQ_LEN)
        self._tokenizer.enable_padding(length=None)
        # Validate the expected input names so downstream slips are
        # loud. BGE-small ONNX usually exports "input_ids",
        # "attention_mask", "token_type_ids".
        self._input_names = {i.name for i in self._session.get_inputs()}
        logger.debug(
            "BGE-small loaded from {} (inputs={}, dim={})",
            mp, sorted(self._input_names), _EMBED_DIM,
        )

    @property
    def dim(self) -> int:
        return _EMBED_DIM

    def embed(
        self, texts: list[str], *, is_query: bool = False,
    ) -> "np.ndarray":
        """Return an (N, 384) L2-normalized float32 matrix.

        `is_query=True` applies BGE's retrieval query prefix; use
        False for documents (entity strings) to match the training
        regime. Empty input → empty (0, 384) matrix so callers can
        concat without a branch."""
        if not texts:
            return np.zeros((0, _EMBED_DIM), dtype=np.float32)
        if is_query:
            texts = [_QUERY_PREFIX + t for t in texts]
        enc = self._tokenizer.encode_batch(texts)
        input_ids = np.array(
            [e.ids for e in enc], dtype=np.int64,
        )
        attention_mask = np.array(
            [e.attention_mask for e in enc], dtype=np.int64,
        )
        feeds: dict[str, np.ndarray] = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)
        outputs = self._session.run(None, feeds)
        # BGE outputs last_hidden_state; use CLS pooling (index 0) per
        # the model card. Some ONNX exports include a pre-pooled head
        # as output[1]; prefer that when present since it encodes the
        # recommended normalization too.
        if len(outputs) >= 2 and outputs[1].ndim == 2:
            pooled = outputs[1]
        else:
            pooled = outputs[0][:, 0, :]
        # L2 normalize so cosine == dot.
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        return (pooled / norms).astype(np.float32)


# ---------------------------------------------------------------------------
# Entity document shape (Phase 8.3 spec §6.8.3)
# ---------------------------------------------------------------------------

def build_entity_document(
    friendly_name: str,
    entity_id: str,
    domain: str,
    device_class: str | None = None,
    area_name: str | None = None,
    floor_name: str | None = None,
    device_name: str | None = None,
    aliases: list[str] | None = None,
) -> str:
    """Canonical text representation used for embedding. Operator sees
    this string verbatim in the WebUI's Candidate retrieval card so
    they can diagnose why a given entity does or doesn't match. Stays
    stable across minor HA cache refreshes — changing its shape
    invalidates the on-disk cached embedding."""
    # Friendly name first — queries prefix a human-readable phrase,
    # and BGE-small weights the leading clause heavily.
    primary = friendly_name or entity_id.split(".", 1)[-1].replace("_", " ")
    parts: list[str] = [primary]
    # Include aliases as a sibling clause so "reading lamp" queries
    # hit entities that only carry the match in their alias list.
    if aliases:
        alias_str = ", ".join(a.strip() for a in aliases if a and a.strip())
        if alias_str:
            parts.append(f"aliases={alias_str}")
    if area_name:
        parts.append(f"area={area_name}")
    if floor_name:
        parts.append(f"floor={floor_name}")
    parts.append(f"domain={domain}")
    if device_class:
        parts.append(f"device_class={device_class}")
    if device_name:
        parts.append(f"device_name={device_name}")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# SemanticIndex — build/retrieve/persist/load
# ---------------------------------------------------------------------------

@dataclass
class SemanticHit:
    """One result of a `retrieve()` call. Kept distinct from
    CandidateMatch so callers can decide whether to convert, and so
    the device-diversity filter in §8.3.3 can operate on a thinner
    struct. Score is cosine similarity in [-1, 1]; realistic values
    are ~0.3 (weak) to ~0.9 (exact)."""
    entity_id: str
    score: float
    document: str = ""
    device_id: str | None = None
    # Phase 8.5 — area / floor metadata so the disambiguator's
    # area-scoped inference can narrow the candidate set without
    # re-hitting the cache.
    area_id: str | None = None
    floor_id: str | None = None


class SemanticIndex:
    """In-memory semantic index over the live EntityCache.

    This class is a skeleton in §8.3.1; §8.3.2 wires build/retrieve/
    persist/load; §8.3.3 adds the device-diversity pass; §8.3.4 plugs
    it into the disambiguator. Kept minimal here so deploys land
    early and the shape is auditable.
    """

    def __init__(
        self,
        cache: Any,  # EntityCache — typed `Any` to avoid circular import.
        *,
        model_path: str | Path = DEFAULT_MODEL_PATH,
        tokenizer_path: str | Path = DEFAULT_TOKENIZER_PATH,
        index_path: str | Path = DEFAULT_INDEX_PATH,
    ) -> None:
        self._cache = cache
        self._model_path = Path(model_path)
        self._tokenizer_path = Path(tokenizer_path)
        self._index_path = Path(index_path)
        self._embedder: Embedder | None = None
        # Parallel arrays — entity_ids[i] ↔ embeddings[i]. Single-
        # writer, many-reader; lock held only during build/rotate.
        self._entity_ids: list[str] = []
        self._documents: list[str] = []
        self._device_ids: list[str | None] = []
        # Phase 8.5 — parallel arrays so retrieve() can filter by
        # area / floor without re-hitting the cache. Populated from
        # build() using the registries loaded via apply_area_registry
        # / apply_floor_registry.
        self._entity_area_ids: list[str | None] = []
        self._entity_floor_ids: list[str | None] = []
        self._embeddings = None  # np.ndarray | None
        # Registry joins. Populated via apply_* methods; missing
        # entries leave the field None and the document omits it.
        self._area_names: dict[str, str] = {}
        self._device_names: dict[str, str] = {}
        self._floor_names: dict[str, str] = {}
        self._area_floor: dict[str, str] = {}  # area_id → floor_id
        # Phase 8.5 — entity-level and device-level area registry joins.
        # Many entities in HA have no direct `area_id` attribute on the
        # state object; their effective area comes from the device.
        # We resolve both at build() time: entity_area_id falls back to
        # device_area_id when the entity doesn't have its own.
        self._entity_reg_areas: dict[str, str] = {}  # entity_id → area_id
        self._device_areas: dict[str, str] = {}      # device_id → area_id
        self._lock = threading.RLock()

    def is_ready(self) -> bool:
        """True iff the model is loaded AND the index has entries."""
        return self._embedder is not None and bool(self._entity_ids)

    def apply_area_registry(self, entries: list[dict[str, Any]]) -> int:
        """Cache area_id → name + floor. Returns rows accepted."""
        n = 0
        with self._lock:
            for e in entries:
                aid = e.get("area_id") or e.get("id")
                if not aid:
                    continue
                self._area_names[str(aid)] = str(e.get("name") or aid)
                fid = e.get("floor_id")
                if fid:
                    self._area_floor[str(aid)] = str(fid)
                n += 1
        return n

    def apply_device_registry(self, entries: list[dict[str, Any]]) -> int:
        """Cache device_id → human-readable name AND device_id →
        area_id so entities without their own area_id can inherit
        from the device at build() time. Returns rows accepted."""
        n = 0
        with self._lock:
            for e in entries:
                did = e.get("id") or e.get("device_id")
                if not did:
                    continue
                did = str(did)
                # Prefer name_by_user when operator has relabeled.
                name = (
                    e.get("name_by_user")
                    or e.get("name")
                    or did
                )
                self._device_names[did] = str(name)
                area = e.get("area_id")
                if area:
                    self._device_areas[did] = str(area)
                n += 1
        return n

    def apply_entity_registry(self, entries: list[dict[str, Any]]) -> int:
        """Capture entity-level area_id from HA's entity_registry. The
        state API doesn't put area_id on most entities — it lives on
        the registry entry (or on the device the entity belongs to).
        Returns rows with an area_id found."""
        n = 0
        with self._lock:
            for e in entries:
                eid = e.get("entity_id")
                aid = e.get("area_id")
                if eid and aid:
                    self._entity_reg_areas[str(eid)] = str(aid)
                    n += 1
        return n

    def apply_floor_registry(self, entries: list[dict[str, Any]]) -> int:
        """Cache floor_id → name. Returns rows accepted. Deferred to
        Phase 8.5 for utterance-side inference; storing now keeps the
        document shape stable across phases."""
        n = 0
        with self._lock:
            for e in entries:
                fid = e.get("floor_id") or e.get("id")
                if not fid:
                    continue
                self._floor_names[str(fid)] = str(e.get("name") or fid)
                n += 1
        return n

    def _ensure_embedder(self) -> Embedder | None:
        """Lazy-load the ONNX model. Returns None when the model files
        are absent — callers fall back to the fuzzy matcher."""
        if self._embedder is not None:
            return self._embedder
        if not is_semantic_retrieval_available(
            self._model_path, self._tokenizer_path,
        ):
            logger.warning(
                "BGE-small not available — semantic retrieval disabled, "
                "fuzzy matcher remains active. model={} tokenizer={}",
                self._model_path, self._tokenizer_path,
            )
            return None
        try:
            self._embedder = Embedder(
                self._model_path, self._tokenizer_path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "BGE-small load failed; semantic retrieval disabled: {}",
                exc,
            )
            return None
        return self._embedder

    # ── Build / retrieve / persist / load (Phase 8.3.2) ─────────

    # Versions feed the on-disk header so a stale persisted index
    # from a previous container version gets rejected cleanly
    # instead of silently feeding the retriever mis-shaped data.
    _SCHEMA_VERSION = "v2"  # Phase 8.5 — added area_ids / floor_ids arrays
    _DOC_VERSION = "doc-v1"
    _MODEL_NAME = "bge-small-en-v1.5"
    _INDEX_HEADER = f"{_SCHEMA_VERSION}|{_MODEL_NAME}|{_DOC_VERSION}"

    def build(self, *, batch_size: int = 32) -> int:
        """Re-embed every entity currently in the cache.

        Synchronous; callers that don't want to block should run this
        on a background thread. Returns the count of entities
        embedded. Clears any previously-loaded index atomically at
        the end — the old data remains queryable until the new batch
        finishes."""
        embedder = self._ensure_embedder()
        if embedder is None:
            return 0
        snap = self._cache.snapshot()
        if not snap:
            logger.info("SemanticIndex.build: cache empty, nothing to embed")
            return 0

        ids: list[str] = []
        documents: list[str] = []
        device_ids: list[str | None] = []
        area_ids: list[str | None] = []
        floor_ids: list[str | None] = []
        for entity in snap:
            # Phase 8.5 — resolve effective area via the cascade:
            # entity.area_id (state attr) → entity_registry.area_id
            # → device_registry.area_id. HA uses the same order.
            area_id = (
                entity.area_id
                or self._entity_reg_areas.get(entity.entity_id)
                or self._device_areas.get(getattr(entity, "device_id", "") or "")
                or None
            )
            area_name = (
                self._area_names.get(area_id or "") if area_id else None
            )
            floor_id = (
                self._area_floor.get(area_id or "") if area_id else None
            )
            floor_name = (
                self._floor_names.get(floor_id or "") if floor_id else None
            )
            device_id = getattr(entity, "device_id", None)
            device_name = (
                self._device_names.get(device_id or "")
                if device_id else None
            )
            doc = build_entity_document(
                friendly_name=entity.friendly_name or "",
                entity_id=entity.entity_id,
                domain=entity.domain,
                device_class=entity.device_class,
                area_name=area_name,
                floor_name=floor_name,
                device_name=device_name,
                aliases=entity.aliases,
            )
            ids.append(entity.entity_id)
            documents.append(doc)
            device_ids.append(device_id if device_id else None)
            area_ids.append(area_id)
            floor_ids.append(floor_id)

        # Batch to keep peak memory bounded on large houses.
        chunks: list[Any] = []
        for i in range(0, len(documents), batch_size):
            chunk = documents[i : i + batch_size]
            chunks.append(embedder.embed(chunk, is_query=False))
        embeddings = (
            np.vstack(chunks) if len(chunks) > 1 else chunks[0]
        )

        with self._lock:
            self._entity_ids = ids
            self._documents = documents
            self._device_ids = device_ids
            self._entity_area_ids = area_ids
            self._entity_floor_ids = floor_ids
            self._embeddings = embeddings
        # logger.success so this ops-relevant signal survives the
        # engine's loguru sink filter (level=SUCCESS) — logger.info
        # gets dropped and made prior bootstrap runs look silent.
        logger.success(
            "SemanticIndex.build: embedded {} entities (dim={})",
            len(ids), _EMBED_DIM,
        )
        return len(ids)

    def retrieve(
        self,
        query: str,
        *,
        k: int = 8,
        domain_filter: list[str] | None = None,
        area_id: str | None = None,
        floor_id: str | None = None,
    ) -> list[SemanticHit]:
        """Return the top-k semantically-closest entity hits.

        Raw cosine ranking only — the device-diversity filter added
        in Phase 8.3.3 wraps this to enforce the operator's
        non-negotiable gates before the results reach the planner.
        Callers that want the full "safe" retrieval should use
        `retrieve_for_planner` (added in 8.3.3) instead.

        Phase 8.5 — optional `area_id` / `floor_id` filter hints.
        When set, only entities matching the hint are considered; the
        filter is AND-composed with `domain_filter` so a query can
        say "dim the downstairs lights" → domain=light AND
        floor_id=floor_main."""
        if not query or not query.strip():
            return []
        with self._lock:
            embeddings = self._embeddings
            ids = list(self._entity_ids)
            documents = list(self._documents)
            device_ids = list(self._device_ids)
            entity_area_ids = list(self._entity_area_ids)
            entity_floor_ids = list(self._entity_floor_ids)
        if embeddings is None or len(ids) == 0:
            return []
        embedder = self._ensure_embedder()
        if embedder is None:
            return []

        q_vec = embedder.embed([query], is_query=True)[0]
        # Both sides L2-normalized → dot == cosine in [-1, 1].
        sims = embeddings @ q_vec

        # Domain filter by entity_id prefix — cheap, avoids re-
        # embedding or keeping a parallel domain array.
        if domain_filter:
            allowed = set(domain_filter)
            mask = np.array(
                [i.split(".", 1)[0] in allowed for i in ids],
                dtype=bool,
            )
            # Mark non-matching positions as -inf so they never
            # surface; doesn't mutate the stored embeddings.
            sims = np.where(mask, sims, -np.inf)

        # Phase 8.5 — area_id / floor_id filter. Entities missing
        # the relevant registry tag (area_id=None on the entity) are
        # excluded from a filtered query: if the operator asked for
        # "downstairs" and an entity has no floor info, it's not
        # credibly "downstairs" either. Parallel-array length must
        # match `ids` — a stale on-disk archive can leave them empty
        # or short; in that case skip the filter so the search
        # degrades gracefully rather than broadcasting onto nothing.
        if area_id and len(entity_area_ids) == len(ids):
            mask = np.array(
                [aid == area_id for aid in entity_area_ids],
                dtype=bool,
            )
            sims = np.where(mask, sims, -np.inf)
        elif area_id:
            logger.warning(
                "SemanticIndex.retrieve: entity_area_ids length "
                "{} != ids length {}; skipping area filter",
                len(entity_area_ids), len(ids),
            )
        if floor_id and len(entity_floor_ids) == len(ids):
            mask = np.array(
                [fid == floor_id for fid in entity_floor_ids],
                dtype=bool,
            )
            sims = np.where(mask, sims, -np.inf)
        elif floor_id:
            logger.warning(
                "SemanticIndex.retrieve: entity_floor_ids length "
                "{} != ids length {}; skipping floor filter",
                len(entity_floor_ids), len(ids),
            )

        # Argpartition for top-k; final sort orders by score desc.
        # Clamp k to the filtered size.
        available = int(np.isfinite(sims).sum())
        take = min(k, available)
        if take <= 0:
            return []
        top_idx = np.argpartition(-sims, take - 1)[:take]
        top_idx = top_idx[np.argsort(-sims[top_idx])]
        has_area = len(entity_area_ids) == len(ids)
        has_floor = len(entity_floor_ids) == len(ids)
        return [
            SemanticHit(
                entity_id=ids[i],
                score=float(sims[i]),
                document=documents[i],
                device_id=device_ids[i],
                area_id=entity_area_ids[i] if has_area else None,
                floor_id=entity_floor_ids[i] if has_floor else None,
            )
            for i in top_idx
        ]

    def persist(self) -> bool:
        """Save embeddings + parallel metadata to disk. Returns True
        on success. Safe to call when the index is empty — nothing
        is written. Uses npz_compressed for a ~2:1 size win."""
        with self._lock:
            if self._embeddings is None or not self._entity_ids:
                return False
            ids = list(self._entity_ids)
            documents = list(self._documents)
            device_ids = [d or "" for d in self._device_ids]
            # Phase 8.5 — area / floor parallel arrays. Empty string
            # stands in for None so numpy's object dtype doesn't
            # need special-case handling on save/load.
            area_ids = [a or "" for a in self._entity_area_ids]
            floor_ids = [f or "" for f in self._entity_floor_ids]
            embeddings = self._embeddings
        try:
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            # np.savez_compressed auto-appends `.npz` when the target
            # filename lacks that suffix — so our tmp has to already
            # end in `.npz` to avoid a mangled filename that never
            # shows up at the rename target. Format: `<stem>.tmp.npz`.
            tmp = self._index_path.parent / (
                self._index_path.stem + ".tmp.npz"
            )
            np.savez_compressed(
                str(tmp),
                header=np.array(self._INDEX_HEADER),
                embeddings=embeddings,
                entity_ids=np.array(ids, dtype=object),
                documents=np.array(documents, dtype=object),
                device_ids=np.array(device_ids, dtype=object),
                area_ids=np.array(area_ids, dtype=object),
                floor_ids=np.array(floor_ids, dtype=object),
            )
            tmp.replace(self._index_path)
            logger.success(
                "SemanticIndex.persist: wrote {} entities to {}",
                len(ids), self._index_path,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SemanticIndex.persist failed: {}", exc,
            )
            return False

    def load(self) -> bool:
        """Restore a persisted index. Returns True on success. False
        means the caller should call `build()` to regenerate — the
        file is missing, from an incompatible schema, or corrupt."""
        if not self._index_path.exists():
            return False
        try:
            with np.load(self._index_path, allow_pickle=True) as data:
                header = str(data["header"])
                if header != self._INDEX_HEADER:
                    logger.info(
                        "SemanticIndex.load: header mismatch "
                        "(disk={!r}, expected={!r}), rebuilding",
                        header, self._INDEX_HEADER,
                    )
                    return False
                embeddings = data["embeddings"]
                ids = [str(x) for x in data["entity_ids"]]
                documents = [str(x) for x in data["documents"]]
                device_ids = [
                    (str(x) if x else None)
                    for x in data["device_ids"]
                ]
                # Phase 8.5 — area / floor parallel arrays. Present on
                # v2+ archives; missing means the .npz is pre-8.5 and
                # the header check below will already reject it.
                area_ids = [
                    (str(x) if x else None)
                    for x in data["area_ids"]
                ]
                floor_ids = [
                    (str(x) if x else None)
                    for x in data["floor_ids"]
                ]
            if embeddings.shape[0] != len(ids):
                logger.warning(
                    "SemanticIndex.load: shape mismatch "
                    "(embeddings={}, ids={}), rebuilding",
                    embeddings.shape, len(ids),
                )
                return False
            with self._lock:
                self._embeddings = embeddings.astype(np.float32)
                self._entity_ids = ids
                self._documents = documents
                self._device_ids = device_ids
                self._entity_area_ids = area_ids
                self._entity_floor_ids = floor_ids
            logger.success(
                "SemanticIndex.load: restored {} entities from {}",
                len(ids), self._index_path,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SemanticIndex.load failed: {}", exc,
            )
            return False

    def size(self) -> int:
        """Number of entities currently indexed."""
        with self._lock:
            return len(self._entity_ids)

    def retrieve_for_planner(
        self,
        query: str,
        *,
        k: int = 8,
        domain_filter: list[str] | None = None,
        area_id: str | None = None,
        floor_id: str | None = None,
        raw_pool: int = 30,
        segment_tokens: tuple[str, ...] = DEFAULT_SEGMENT_TOKENS,
        ignore_segments: bool = True,
    ) -> list[SemanticHit]:
        """Phase 8.3.3 — retrieve with the device-diversity gate
        applied. This is the method the disambiguator should call;
        raw cosine-only `retrieve()` is exposed for diagnostics /
        the WebUI preview card.

        The raw pool (`raw_pool=30`) is intentionally larger than
        `k` so the diversity filter has room to drop sibling
        segments without leaving top-K short.

        `ignore_segments` defaults to True per operator request:
        entities matching the segment-token pattern are dropped
        entirely, since operators control whole lamps or scenes
        rather than individual segments.

        Phase 8.5 — optional `area_id` / `floor_id` filter hints get
        passed through to the base retrieve()."""
        raw = self.retrieve(
            query, k=max(raw_pool, k),
            domain_filter=domain_filter,
            area_id=area_id,
            floor_id=floor_id,
        )
        return apply_device_diversity(
            raw,
            utterance=query,
            top_k=k,
            segment_tokens=segment_tokens,
            cache=self._cache,
            ignore_segments=ignore_segments,
        )

    # -- Phase 8.5 — registry accessors for utterance-side inference ----

    def area_names(self) -> dict[str, str]:
        """Snapshot of {area_id: name} from the area registry. Used
        by the utterance-side inference module to resolve spoken
        keywords into concrete ids."""
        with self._lock:
            return dict(self._area_names)

    def floor_names(self) -> dict[str, str]:
        """Snapshot of {floor_id: name} from the floor registry."""
        with self._lock:
            return dict(self._floor_names)


__all__ = [
    "DEFAULT_INDEX_PATH",
    "DEFAULT_MODEL_PATH",
    "DEFAULT_SEGMENT_TOKENS",
    "DEFAULT_TOKENIZER_PATH",
    "Embedder",
    "PER_DEVICE_CAP",
    "SemanticHit",
    "SemanticIndex",
    "apply_device_diversity",
    "build_entity_document",
    "is_semantic_retrieval_available",
]
