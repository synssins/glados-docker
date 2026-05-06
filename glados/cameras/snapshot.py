"""HA camera-snapshot fetch.

Single function: ``fetch_snapshot(entity_id, *, ha_url, ha_token) -> bytes``.
Errors raise ``CameraSnapshotError`` with the URL and cause.
"""

from __future__ import annotations

import requests
from loguru import logger


class CameraSnapshotError(RuntimeError):
    """HA snapshot fetch failed."""


def fetch_snapshot(
    entity_id: str,
    *,
    ha_url: str,
    ha_token: str,
    timeout_s: float = 8.0,
) -> bytes:
    """GET HA's ``/api/camera_proxy/<entity_id>``; return body bytes."""
    base = ha_url.rstrip("/")
    url = f"{base}/api/camera_proxy/{entity_id}"
    headers = {"Authorization": f"Bearer {ha_token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout_s)
    except requests.RequestException as exc:
        raise CameraSnapshotError(
            f"HA snapshot unreachable at {ha_url} for {entity_id}: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise CameraSnapshotError(
            f"HA snapshot for {entity_id} returned {resp.status_code}: {resp.text[:200]}"
        )

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if not ctype.startswith("image/"):
        raise CameraSnapshotError(
            f"HA snapshot for {entity_id} returned non-image content-type {ctype!r}"
        )

    return resp.content
