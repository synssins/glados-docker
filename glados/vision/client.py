"""OpenAI-multimodal client for the ``llm_vision`` service slot.

Single function: ``describe_images(images, prompt) -> str``. Pure I/O.
No retries, no fallbacks (per ``feedback_no_silent_fallback.md``).
The slot URL/model/api_key live in ``cfg.services.llm_vision`` and are
operator-configurable via the WebUI services tab — never hardcoded.
"""

from __future__ import annotations

import base64
from typing import Any

import requests
from loguru import logger


class VisionClientError(RuntimeError):
    """Surfaces every VLM-call failure with the URL and cause inline."""


def _get_slot() -> Any:
    # Local import — cfg load pulls in much of the engine; keep it lazy
    # so test mocks can replace this hook without dragging in the world.
    from glados.core.config_store import cfg
    return cfg.services.llm_vision


_MAX_DIMENSION = 1280
"""Largest edge (px) we send to the VLM. The model itself auto-rescales
beyond ~1800px, but our llama.cpp upstream rejects payloads >~1MB at the
HTTP layer (TCP RST, no log). Resizing to <=1280 keeps the base64 body
under ~600KB on a typical 1.5MP source. Pure quality optimization for the
VLM substrate -- no information loss the model wouldn't have discarded
itself."""


def _maybe_resize(image: bytes, mime: str) -> tuple[bytes, str]:
    """If ``image`` exceeds ``_MAX_DIMENSION`` on either edge, resize it
    in-process and re-encode as JPEG (smaller than PNG/WebP). Returns the
    possibly-rewritten ``(bytes, mime)``. Failures fall through unchanged
    -- caller still gets the original payload, just at risk of upstream
    rejection. Logged at WARNING so the failure mode is visible.
    """
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(image))
        w, h = img.size
        if max(w, h) <= _MAX_DIMENSION:
            return image, mime
        # PIL's thumbnail preserves aspect ratio in-place
        img.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION), Image.Resampling.LANCZOS)
        # Always re-encode as JPEG -- it's smaller than PNG/WebP for photos.
        out = BytesIO()
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(out, format="JPEG", quality=85, optimize=True)
        new_bytes = out.getvalue()
        logger.info(
            "vision: resized {}x{} -> {}x{}, {} -> {} bytes",
            w, h, *img.size, len(image), len(new_bytes),
        )
        return new_bytes, "image/jpeg"
    except Exception as exc:  # noqa: BLE001
        logger.warning("vision: resize failed ({}); sending original", exc)
        return image, mime


def _b64_data_url(image: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"


def describe_images(
    images: list[bytes],
    prompt: str,
    *,
    mime: str = "image/jpeg",
    timeout: float = 30.0,
) -> str:
    """POST OpenAI-multimodal to ``llm_vision``; return the model's text."""
    if not images:
        raise ValueError("describe_images requires at least one image")

    slot = _get_slot()
    # Use compose_endpoint so a slot URL that already carries
    # /v1/chat/completions (which is how operators usually configure the
    # WebUI services tab) doesn't get double-suffixed into nonsense.
    from glados.core.url_utils import compose_endpoint
    url = compose_endpoint(slot.url, "/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    # ServiceEndpoint may not expose api_key on every slot model — read it
    # defensively. Live slots in cfg.services.* don't carry api_key today;
    # if a future model adds one, this picks it up automatically.
    _api_key = getattr(slot, "api_key", None)
    if _api_key:
        headers["Authorization"] = f"Bearer {_api_key}"

    parts: list[dict[str, Any]] = []
    for img in images:
        resized, eff_mime = _maybe_resize(img, mime)
        parts.append({
            "type": "image_url",
            "image_url": {"url": _b64_data_url(resized, eff_mime)},
        })
    parts.append({"type": "text", "text": prompt})

    body = {
        "model": slot.model,
        "messages": [{"role": "user", "content": parts}],
        "stream": False,
    }

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise VisionClientError(f"vision endpoint {slot.url} failed: {exc}") from exc

    if resp.status_code != 200:
        raise VisionClientError(
            f"vision endpoint {slot.url} returned {resp.status_code}: {resp.text[:500]}"
        )

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise VisionClientError(f"vision endpoint {slot.url} bad response: {exc}") from exc
