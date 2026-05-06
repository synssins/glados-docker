"""Unit tests for glados/cameras/discovery.py."""

from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import pytest

from glados.cameras.discovery import (
    CameraDiscovery,
    CameraDiscoveryError,
)


HA_STATES = [
    {"entity_id": "camera.backyard_high", "attributes": {"friendly_name": "Backyard High"}},
    {"entity_id": "camera.front_door_high", "attributes": {"friendly_name": "Front Door"}},
    {"entity_id": "light.kitchen", "attributes": {"friendly_name": "Kitchen"}},
]


def _ok_states() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = HA_STATES
    return resp


def test_list_cameras_filters_to_camera_domain():
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="t", ttl_s=60.0)
    with patch("glados.cameras.discovery.requests.get", return_value=_ok_states()):
        cams = disco.list_cameras()
    assert ("camera.backyard_high", "Backyard High") in cams
    assert ("camera.front_door_high", "Front Door") in cams
    assert all(eid.startswith("camera.") for eid, _ in cams)


def test_repeated_calls_within_ttl_use_cache():
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="t", ttl_s=60.0)
    with patch("glados.cameras.discovery.requests.get", return_value=_ok_states()) as g:
        disco.list_cameras()
        disco.list_cameras()
        disco.list_cameras()
    assert g.call_count == 1


def test_call_after_ttl_refreshes():
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="t", ttl_s=0.01)
    with patch("glados.cameras.discovery.requests.get", return_value=_ok_states()) as g:
        disco.list_cameras()
        time.sleep(0.05)
        disco.list_cameras()
    assert g.call_count == 2


def test_resolve_camera_name_friendly_match():
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="t")
    with patch("glados.cameras.discovery.requests.get", return_value=_ok_states()):
        assert disco.resolve_camera_name("back yard") == "camera.backyard_high"
        assert disco.resolve_camera_name("BACKYARD") == "camera.backyard_high"
        assert disco.resolve_camera_name("front door") == "camera.front_door_high"


def test_resolve_camera_name_entity_id_match():
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="t")
    with patch("glados.cameras.discovery.requests.get", return_value=_ok_states()):
        assert disco.resolve_camera_name("backyard_high") == "camera.backyard_high"


def test_resolve_camera_name_miss_returns_none():
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="t")
    with patch("glados.cameras.discovery.requests.get", return_value=_ok_states()):
        assert disco.resolve_camera_name("garage") is None


def test_ha_non_200_raises():
    err = MagicMock()
    err.status_code = 401
    err.text = "unauthorized"
    disco = CameraDiscovery(ha_url="http://ha:8123", ha_token="bad")
    with patch("glados.cameras.discovery.requests.get", return_value=err):
        with pytest.raises(CameraDiscoveryError) as exc:
            disco.list_cameras()
    assert "401" in str(exc.value)
