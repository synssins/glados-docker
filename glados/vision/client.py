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
    url = slot.url.rstrip("/") + "/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if slot.api_key:
        headers["Authorization"] = f"Bearer {slot.api_key}"

    parts: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": _b64_data_url(img, mime)}}
        for img in images
    ]
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
