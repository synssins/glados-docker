"""HA camera-entity discovery cache.

Uses HA's HTTP ``/api/states`` (NOT WebSocket) — this slice ships
without the shared HAWebSocketHub that Slice 3 introduces. Polling is
fine: cameras change rarely and a 60-second TTL is well within the
operator's expectation of 'operator added a new camera, want to ask
about it'.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import requests
from loguru import logger


class CameraDiscoveryError(RuntimeError):
    """HA states fetch failed."""


@dataclass(frozen=True)
class _CamEntry:
    entity_id: str
    friendly_name: str


def _normalize(text: str) -> str:
    return text.casefold().replace("_", " ").replace(".", " ").strip()


class CameraDiscovery:
    """Per-process cache of HA camera entities.

    Construct once (the chat tool dispatch caches the singleton) and
    reuse across requests. Thread-safe — list_cameras() guards the
    refresh path with a lock.
    """

    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        *,
        ttl_s: float = 60.0,
        timeout_s: float = 5.0,
    ) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._ha_token = ha_token
        self._ttl_s = ttl_s
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._cache: list[_CamEntry] = []
        self._fetched_at: float = 0.0

    def _fetch(self) -> list[_CamEntry]:
        url = f"{self._ha_url}/api/states"
        headers = {"Authorization": f"Bearer {self._ha_token}"}
        try:
            resp = requests.get(url, headers=headers, timeout=self._timeout_s)
        except requests.RequestException as exc:
            raise CameraDiscoveryError(f"HA states unreachable at {self._ha_url}: {exc}") from exc
        if resp.status_code != 200:
            raise CameraDiscoveryError(
                f"HA states returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            states = resp.json()
        except ValueError as exc:
            raise CameraDiscoveryError(f"HA states bad JSON: {exc}") from exc

        cams: list[_CamEntry] = []
        for s in states:
            eid = s.get("entity_id", "")
            if not eid.startswith("camera."):
                continue
            friendly = s.get("attributes", {}).get("friendly_name") or eid
            cams.append(_CamEntry(entity_id=eid, friendly_name=friendly))
        return cams

    def list_cameras(self) -> list[tuple[str, str]]:
        """Return ``[(entity_id, friendly_name), ...]``. Refreshes if TTL elapsed."""
        with self._lock:
            now = time.time()
            if not self._cache or (now - self._fetched_at) >= self._ttl_s:
                self._cache = self._fetch()
                self._fetched_at = now
            return [(c.entity_id, c.friendly_name) for c in self._cache]

    def resolve_camera_name(self, query: str) -> str | None:
        """Return the entity_id whose friendly_name or short id best matches ``query``.

        Match strategy (in order):
          1. Exact normalized friendly_name match.
          2. Substring match against normalized friendly_name.
          3. Substring match against entity_id stripped of ``camera.`` prefix
             (so 'backyard_high' resolves directly).
        First hit wins. ``None`` if nothing matches.
        """
        cams = self.list_cameras()
        q = _normalize(query)
        if not q:
            return None

        # Pass 1: exact friendly_name (normalized)
        for eid, friendly in cams:
            if _normalize(friendly) == q:
                return eid

        # Pass 2: substring against friendly_name
        for eid, friendly in cams:
            if q in _normalize(friendly):
                return eid

        # Pass 2b: space-collapsed match (handles "back yard" → "backyard high")
        q_nospace = q.replace(" ", "")
        for eid, friendly in cams:
            if q_nospace in _normalize(friendly).replace(" ", ""):
                return eid

        # Pass 3: substring against entity_id sans prefix
        for eid, _friendly in cams:
            short = _normalize(eid.replace("camera.", "", 1))
            if q in short:
                return eid

        return None
