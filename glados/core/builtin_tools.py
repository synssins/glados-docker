"""Built-in function-calling tools — Phase 8.3.4b.

These are registered in the OpenAI-style `tools` array the Tier 3
chat path sends to Ollama, alongside whatever the MCP manager
exposes. Unlike MCP-server tools, these run IN-PROCESS against the
container's own SemanticIndex + EntityCache, so they're always
available even when no remote MCP server is configured.

Two tools ship in 8.3.4b:

  - `search_entities(query, top_k=8, domain_filter=None)` — returns
    the top-K semantic retrieval results for an arbitrary query,
    post-device-diversity. This is what the Tier 3 planner uses
    when Tier 2's candidate list was insufficient ("the thing in
    the office that makes light but not the overheads").

  - `get_entity_details(entity_id)` — returns the full state +
    attributes of a named entity. Used as a follow-up after
    search_entities narrows things down.

Both functions return plain strings (single JSON body) so the
tool-call return path at `_stream_chat_sse_impl` treats them the
same as an MCP tool result. The diversity filter runs on
search_entities output — device-segment storms are suppressed
even when the LLM invokes the tool mid-reasoning.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# Tool name constants — stable strings that the tool-call router uses.
# ---------------------------------------------------------------------------

TOOL_SEARCH_ENTITIES = "search_entities"
TOOL_GET_ENTITY_DETAILS = "get_entity_details"
TOOL_LOOK_AT_CAMERA = "look_at_camera"

_BUILTIN_TOOL_NAMES: frozenset[str] = frozenset({
    TOOL_SEARCH_ENTITIES,
    TOOL_GET_ENTITY_DETAILS,
})

_IMAGE_YIELDING_TOOL_NAMES: frozenset[str] = frozenset({TOOL_LOOK_AT_CAMERA})


def is_builtin_tool(tool_name: str) -> bool:
    """Router predicate. Returns True if `tool_name` maps to one of
    the in-process tools defined here rather than an MCP-server tool."""
    return tool_name in _BUILTIN_TOOL_NAMES


def is_image_yielding_tool(tool_name: str) -> bool:
    """Router predicate. Returns True if the tool emits image bytes
    out-of-band (via SSE event:image) in addition to its text result."""
    return tool_name in _IMAGE_YIELDING_TOOL_NAMES


# ---------------------------------------------------------------------------
# OpenAI-style tool definitions — injected into the `tools` array the
# chat request sends to Ollama.
# ---------------------------------------------------------------------------

def get_builtin_tool_definitions() -> list[dict[str, Any]]:
    """Return built-in tool definitions.

    Kept as a function rather than a module-level constant so the
    description text can evolve without import-order gotchas — and
    so callers that introspect always read the current values."""
    return [
        {
            "type": "function",
            "function": {
                "name": TOOL_SEARCH_ENTITIES,
                "description": (
                    "Search Home Assistant entities semantically by a "
                    "natural-language query. Returns the top-K most "
                    "relevant entities (friendly name, entity_id, "
                    "domain, area, device name) with cosine similarity "
                    "scores. Results are already filtered for device "
                    "diversity — multi-segment LED strip siblings are "
                    "collapsed to one representative unless the query "
                    "explicitly names a segment. Call this when the "
                    "user's device intent is unclear or you need to "
                    "find entities beyond those in the initial "
                    "candidate list."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Natural-language description of what to "
                                "find. E.g. 'reading lamp in the office', "
                                "'all kitchen lights', 'bedroom ceiling'."
                            ),
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "How many candidates to return (default 8, max 20).",
                            "default": 8,
                        },
                        "domain_filter": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional: restrict results to specific HA "
                                "domains like ['light','switch']."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": TOOL_GET_ENTITY_DETAILS,
                "description": (
                    "Return the current state and attributes of a "
                    "specific Home Assistant entity. Use this after "
                    "search_entities narrows the target, or when the "
                    "user references an entity you already know the "
                    "entity_id for and need state data (brightness, "
                    "color, temperature, etc.) to reason about it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entity_id": {
                            "type": "string",
                            "description": (
                                "Fully-qualified HA entity id. E.g. "
                                "'light.task_lamp_one'."
                            ),
                        },
                    },
                    "required": ["entity_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": TOOL_LOOK_AT_CAMERA,
                "description": (
                    "Look at a Home Assistant camera and describe what is "
                    "visible. Use this when the user asks 'what do you see' "
                    "or asks about a specific camera by name. The function "
                    "fetches a snapshot, runs a vision model, and returns a "
                    "short scene description. The snapshot itself is rendered "
                    "in the UI separately — the chat reply should reference "
                    "the description naturally."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_name": {
                            "type": "string",
                            "description": (
                                "Friendly name or partial entity_id of the "
                                "camera (e.g. 'back yard', 'front door', "
                                "'backyard_high'). Case-insensitive."
                            ),
                        },
                    },
                    "required": ["camera_name"],
                },
            },
        },
    ]


def get_image_yielding_tool_definitions() -> list[dict[str, Any]]:
    """Return only the image-yielding builtin tools (currently:
    ``look_at_camera``).

    Distinct from ``get_builtin_tool_definitions()`` because the chat
    path injects HA-shape tools (``search_entities``,
    ``get_entity_details``) only on home-command-shape queries — but
    image-yielding tools fire on chat-shape questions like *"What do
    you see in the back yard?"* which never trip ``looks_like_home_command``.
    Callers append this list unconditionally on the chat lane.
    """
    return [
        d for d in get_builtin_tool_definitions()
        if is_image_yielding_tool(d["function"]["name"])
    ]


# ---------------------------------------------------------------------------
# Tool implementations. Each returns a JSON string so the chat loop
# can append it as `{"role": "tool", "content": <str>}` without
# additional serialization.
# ---------------------------------------------------------------------------

def _search_entities(args: dict[str, Any]) -> str:
    """Delegates to the live SemanticIndex on the disambiguator
    singleton. Falls back to the fuzzy EntityCache path when the
    retriever isn't loaded — same fallback contract the
    disambiguator itself uses."""
    from glados.ha import get_cache
    from glados.ha.semantic_index import (
        DEFAULT_SEGMENT_TOKENS, apply_device_diversity,
    )
    from glados.intent import get_disambiguator

    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})
    top_k = int(args.get("top_k") or 8)
    top_k = max(1, min(top_k, 20))
    domain_filter = args.get("domain_filter")
    if domain_filter is not None and not isinstance(domain_filter, list):
        return json.dumps({
            "error": "domain_filter must be a list of strings",
        })

    disambig = get_disambiguator()
    idx = getattr(disambig, "_semantic_index", None) if disambig else None
    rules = getattr(disambig, "_rules", None) if disambig else None
    extras = tuple(getattr(rules, "extra_segment_tokens", []) or ())
    ignore_seg = bool(getattr(rules, "ignore_segments", True))

    if idx is not None and idx.is_ready():
        try:
            hits = idx.retrieve_for_planner(
                query,
                k=top_k,
                domain_filter=domain_filter,
                segment_tokens=DEFAULT_SEGMENT_TOKENS + extras,
                ignore_segments=ignore_seg,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "builtin search_entities: retriever raised: {}", exc,
            )
            hits = []
        if hits:
            return json.dumps({
                "query": query,
                "results": [
                    _hit_to_result_dict(h, cache=idx._cache) for h in hits
                ],
            })

    # Fallback: fuzzy matcher via the live entity cache.
    cache = get_cache()
    if cache is None:
        return json.dumps({
            "error": "entity cache not initialised",
            "query": query,
        })
    fuzzy = cache.get_candidates(
        query,
        domain_filter=domain_filter,
        limit=top_k,
        ignore_segments=ignore_seg,
        segment_tokens=DEFAULT_SEGMENT_TOKENS + extras,
    )
    results = []
    for c in fuzzy:
        e = c.entity
        results.append({
            "entity_id": e.entity_id,
            "name": e.friendly_name or e.entity_id,
            "domain": e.domain,
            "state": e.state,
            "area_id": e.area_id,
            "score": round(float(c.score) / 100.0, 4),  # scale to ~[0,1]
            "device_id": getattr(c, "device_id", None),
        })
    return json.dumps({"query": query, "results": results, "fallback": "fuzzy"})


def _get_entity_details(args: dict[str, Any]) -> str:
    from glados.ha import get_cache

    eid = str(args.get("entity_id") or "").strip()
    if not eid:
        return json.dumps({"error": "entity_id is required"})
    cache = get_cache()
    if cache is None:
        return json.dumps({"error": "entity cache not initialised"})
    entity = cache.get(eid)
    if entity is None:
        return json.dumps({"error": f"entity not found: {eid}"})
    return json.dumps({
        "entity_id": entity.entity_id,
        "friendly_name": entity.friendly_name,
        "domain": entity.domain,
        "state": entity.state,
        "device_class": entity.device_class,
        "area_id": entity.area_id,
        "device_id": getattr(entity, "device_id", None),
        "aliases": list(entity.aliases),
        "attributes": _scrub_attributes(entity.attributes),
    })


def _scrub_attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop attributes that bloat the tool response without helping
    the LLM reason. Keeps the decision-relevant fields (brightness,
    color, temp, position) and drops registry paperwork (icons,
    entity pictures, raw Z-Wave maps)."""
    drop_prefixes = (
        "entity_picture", "icon", "zwave_js", "hs_color", "xy_color",
        "rgb_color", "rgbw_color", "rgbww_color",
    )
    drop_exact = {
        "entity_id", "friendly_name", "supported_features",
        "icon_template", "templates", "restored", "custom_ui",
    }
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if k in drop_exact:
            continue
        if any(k.startswith(p) for p in drop_prefixes):
            continue
        # Shrink giant lists to their lengths so we don't blow up
        # the tool response context.
        if isinstance(v, list) and len(v) > 20:
            out[k] = f"<{len(v)} items omitted>"
        else:
            out[k] = v
    return out


def _hit_to_result_dict(hit: Any, *, cache: Any) -> dict[str, Any]:
    """Convert a SemanticHit to the result shape the LLM consumes."""
    eid = hit.entity_id
    entity = cache.get(eid) if cache else None
    out: dict[str, Any] = {
        "entity_id": eid,
        "score": round(float(hit.score), 4),
        "device_id": hit.device_id,
    }
    if entity is not None:
        out["name"] = entity.friendly_name or eid
        out["domain"] = entity.domain
        out["state"] = entity.state
        out["area_id"] = entity.area_id
    return out


# ---------------------------------------------------------------------------
# Image-yielding tool support — look_at_camera.
# ---------------------------------------------------------------------------

# Module-level imports so test patches on these names work correctly.
from glados.cameras.snapshot import fetch_snapshot, CameraSnapshotError  # noqa: E402
from glados.vision.client import describe_images, VisionClientError  # noqa: E402


@dataclass(frozen=True)
class ImageEmission:
    """Out-of-band image bytes emitted alongside a tool's text result.

    The SSE handler picks this up and writes an ``event: image`` chunk
    keyed by ``tool_call_id`` so the WebUI can render the snapshot in
    the assistant bubble. The bytes never enter LLM context.
    """

    image_bytes: bytes
    mime: str
    tool_name: str


# Cached per-process discovery instance. First call constructs from cfg;
# subsequent calls reuse the same cache. Tests patch this hook.
_camera_discovery: object = None


def _get_camera_discovery() -> object:
    global _camera_discovery
    if _camera_discovery is None:
        from glados.core.config_store import cfg
        from glados.cameras.discovery import CameraDiscovery
        _camera_discovery = CameraDiscovery(
            ha_url=cfg.ha_url,
            ha_token=cfg.ha_token,
        )
    return _camera_discovery


def _look_at_camera(arguments: dict[str, Any]) -> tuple[str, "ImageEmission | None"]:
    """Resolve camera -> snapshot -> VLM. Returns (json_text_result, optional_image_emission).

    Errors are returned as JSON ``{"error": "..."}`` strings (visible to
    the chat LLM, which relays them) — never raised. Emission is None on
    any failure path so the SSE handler doesn't push a phantom image.
    """
    from glados.core.config_store import cfg

    name = (arguments or {}).get("camera_name", "").strip()
    if not name:
        return json.dumps({"error": "camera_name is required"}), None

    disco = _get_camera_discovery()
    try:
        entity_id = disco.resolve_camera_name(name)
    except Exception as exc:
        return json.dumps({"error": f"camera discovery failed: {exc}"}), None

    if not entity_id:
        try:
            avail = ", ".join(f"{f} ({eid})" for eid, f in disco.list_cameras())
        except Exception:
            avail = "(camera list unavailable)"
        return (
            json.dumps({"error": f'no camera matched "{name}". Available: {avail}'}),
            None,
        )

    try:
        image_bytes = fetch_snapshot(
            entity_id, ha_url=cfg.ha_url, ha_token=cfg.ha_token,
        )
    except CameraSnapshotError as exc:
        return json.dumps({"error": f"HA snapshot failed: {exc}"}), None

    try:
        description = describe_images(
            [image_bytes],
            "Describe what you see in this image in 1-3 sentences.",
        )
    except VisionClientError as exc:
        return json.dumps({"error": str(exc)}), None

    return (
        json.dumps({"description": description}),
        ImageEmission(image_bytes=image_bytes, mime="image/jpeg", tool_name=TOOL_LOOK_AT_CAMERA),
    )


def invoke_image_yielding_tool(
    tool_name: str, arguments: dict[str, Any],
) -> tuple[str, "ImageEmission | None"]:
    """Dispatch entry point for tools that emit out-of-band image bytes.

    Returns ``(json_result_string, optional_emission)``. The
    ``json_result_string`` is what gets appended to the LLM's
    conversation as the ``{"role":"tool", ...}`` content. The emission
    (if any) is what the SSE handler turns into an ``event: image``
    chunk for the WebUI.
    """
    if tool_name == TOOL_LOOK_AT_CAMERA:
        return _look_at_camera(arguments)
    return json.dumps({"error": f"unknown image-yielding tool: {tool_name}"}), None


# ---------------------------------------------------------------------------
# Public invocation entry — called from the Tier 3 tool-call router.
# ---------------------------------------------------------------------------

def invoke_builtin_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    """Dispatch to the appropriate built-in tool. Returns a JSON
    string suitable for appending as a `{"role": "tool", ...}`
    message to the chat history."""
    if tool_name == TOOL_SEARCH_ENTITIES:
        return _search_entities(arguments)
    if tool_name == TOOL_GET_ENTITY_DETAILS:
        return _get_entity_details(arguments)
    return json.dumps({"error": f"unknown built-in tool: {tool_name}"})


__all__ = [
    "TOOL_GET_ENTITY_DETAILS",
    "TOOL_LOOK_AT_CAMERA",
    "TOOL_SEARCH_ENTITIES",
    "ImageEmission",
    "get_builtin_tool_definitions",
    "get_image_yielding_tool_definitions",
    "invoke_builtin_tool",
    "invoke_image_yielding_tool",
    "is_builtin_tool",
    "is_image_yielding_tool",
]
