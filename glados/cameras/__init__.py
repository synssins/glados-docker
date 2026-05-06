"""HA camera discovery and snapshot helpers."""

from .discovery import CameraDiscovery, CameraDiscoveryError
from .snapshot import fetch_snapshot, CameraSnapshotError

__all__ = [
    "CameraDiscovery",
    "CameraDiscoveryError",
    "fetch_snapshot",
    "CameraSnapshotError",
]
