"""Unit tests for glados/cameras/snapshot.py."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
import requests

from glados.cameras.snapshot import fetch_snapshot, CameraSnapshotError


def _ok_jpeg(body: bytes = b"\xff\xd8\xff\xe0jpeg") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": "image/jpeg"}
    resp.content = body
    return resp


def test_returns_bytes_on_200():
    with patch("glados.cameras.snapshot.requests.get", return_value=_ok_jpeg(b"abc")):
        out = fetch_snapshot("camera.backyard_high", ha_url="http://ha:8123", ha_token="t")
    assert out == b"abc"


def test_non_200_raises():
    err = MagicMock()
    err.status_code = 404
    err.headers = {}
    err.text = "not found"
    with patch("glados.cameras.snapshot.requests.get", return_value=err):
        with pytest.raises(CameraSnapshotError) as exc:
            fetch_snapshot("camera.missing", ha_url="http://ha:8123", ha_token="t")
    assert "404" in str(exc.value)
    assert "camera.missing" in str(exc.value)


def test_non_image_content_type_raises():
    bad = MagicMock()
    bad.status_code = 200
    bad.headers = {"Content-Type": "text/html"}
    bad.content = b"<html>"
    with patch("glados.cameras.snapshot.requests.get", return_value=bad):
        with pytest.raises(CameraSnapshotError) as exc:
            fetch_snapshot("camera.backyard_high", ha_url="http://ha:8123", ha_token="t")
    assert "text/html" in str(exc.value)


def test_timeout_raises():
    with patch("glados.cameras.snapshot.requests.get", side_effect=requests.Timeout("slow")):
        with pytest.raises(CameraSnapshotError) as exc:
            fetch_snapshot("camera.backyard_high", ha_url="http://ha:8123", ha_token="t")
    assert "http://ha:8123" in str(exc.value)
