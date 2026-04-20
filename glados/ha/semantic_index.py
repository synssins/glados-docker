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
        self._embeddings = None  # np.ndarray | None
        # Registry joins. Populated via apply_* methods; missing
        # entries leave the field None and the document omits it.
        self._area_names: dict[str, str] = {}
        self._device_names: dict[str, str] = {}
        self._floor_names: dict[str, str] = {}
        self._area_floor: dict[str, str] = {}  # area_id → floor_id
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
        """Cache device_id → human-readable name. Returns rows accepted."""
        n = 0
        with self._lock:
            for e in entries:
                did = e.get("id") or e.get("device_id")
                if not did:
                    continue
                # Prefer name_by_user when operator has relabeled.
                name = (
                    e.get("name_by_user")
                    or e.get("name")
                    or str(did)
                )
                self._device_names[str(did)] = str(name)
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
    _SCHEMA_VERSION = "v1"
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
        for entity in snap:
            area_name = (
                self._area_names.get(entity.area_id or "")
                if entity.area_id else None
            )
            floor_id = (
                self._area_floor.get(entity.area_id or "")
                if entity.area_id else None
            )
            floor_name = (
                self._floor_names.get(floor_id or "")
                if floor_id else None
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
            self._embeddings = embeddings
        logger.info(
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
    ) -> list[SemanticHit]:
        """Return the top-k semantically-closest entity hits.

        Raw cosine ranking only — the device-diversity filter added
        in Phase 8.3.3 wraps this to enforce the operator's
        non-negotiable gates before the results reach the planner.
        Callers that want the full "safe" retrieval should use
        `retrieve_for_planner` (added in 8.3.3) instead."""
        if not query or not query.strip():
            return []
        with self._lock:
            embeddings = self._embeddings
            ids = list(self._entity_ids)
            documents = list(self._documents)
            device_ids = list(self._device_ids)
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

        # Argpartition for top-k; final sort orders by score desc.
        # Clamp k to the filtered size.
        available = int(np.isfinite(sims).sum())
        take = min(k, available)
        if take <= 0:
            return []
        top_idx = np.argpartition(-sims, take - 1)[:take]
        top_idx = top_idx[np.argsort(-sims[top_idx])]
        return [
            SemanticHit(
                entity_id=ids[i],
                score=float(sims[i]),
                document=documents[i],
                device_id=device_ids[i],
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
            )
            tmp.replace(self._index_path)
            logger.info(
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
            logger.info(
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


__all__ = [
    "DEFAULT_INDEX_PATH",
    "DEFAULT_MODEL_PATH",
    "DEFAULT_TOKENIZER_PATH",
    "Embedder",
    "SemanticHit",
    "SemanticIndex",
    "build_entity_document",
    "is_semantic_retrieval_available",
]
