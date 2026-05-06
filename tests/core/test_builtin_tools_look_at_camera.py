"""Unit tests for the look_at_camera builtin tool."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from glados.core.builtin_tools import (
    TOOL_LOOK_AT_CAMERA,
    get_builtin_tool_definitions,
    invoke_image_yielding_tool,
    is_image_yielding_tool,
)


def test_predicate_matches_only_look_at_camera():
    assert is_image_yielding_tool("look_at_camera") is True
    assert is_image_yielding_tool("search_entities") is False
    assert is_image_yielding_tool("anything") is False


def test_definition_present_in_builtin_list():
    names = [d["function"]["name"] for d in get_builtin_tool_definitions()]
    assert TOOL_LOOK_AT_CAMERA in names


def test_happy_path_returns_description_and_image():
    fake_jpeg = b"\xff\xd8\xff\xe0pic"
    fake_disco = MagicMock()
    fake_disco.resolve_camera_name.return_value = "camera.backyard_high"
    with patch("glados.core.builtin_tools._get_camera_discovery", return_value=fake_disco), \
         patch("glados.core.builtin_tools.fetch_snapshot", return_value=fake_jpeg), \
         patch("glados.core.builtin_tools.describe_images", return_value="a tabby cat"):
        result_json, emission = invoke_image_yielding_tool(
            "look_at_camera", {"camera_name": "back yard"}
        )
    parsed = json.loads(result_json)
    assert parsed == {"description": "a tabby cat"}
    assert emission is not None
    assert emission.image_bytes == fake_jpeg
    assert emission.mime == "image/jpeg"


def test_missing_camera_returns_error_no_emission():
    fake_disco = MagicMock()
    fake_disco.resolve_camera_name.return_value = None
    fake_disco.list_cameras.return_value = [
        ("camera.front_door_high", "Front Door"),
        ("camera.backyard_high", "Backyard High"),
    ]
    with patch("glados.core.builtin_tools._get_camera_discovery", return_value=fake_disco):
        result_json, emission = invoke_image_yielding_tool(
            "look_at_camera", {"camera_name": "garage"}
        )
    parsed = json.loads(result_json)
    assert "error" in parsed
    assert "garage" in parsed["error"]
    assert "Front Door" in parsed["error"]  # available list helps the LLM relay
    assert emission is None


def test_snapshot_failure_returns_error_no_emission():
    from glados.cameras.snapshot import CameraSnapshotError
    fake_disco = MagicMock()
    fake_disco.resolve_camera_name.return_value = "camera.backyard_high"
    with patch("glados.core.builtin_tools._get_camera_discovery", return_value=fake_disco), \
         patch("glados.core.builtin_tools.fetch_snapshot",
               side_effect=CameraSnapshotError("404 not found")):
        result_json, emission = invoke_image_yielding_tool(
            "look_at_camera", {"camera_name": "back yard"}
        )
    parsed = json.loads(result_json)
    assert "error" in parsed
    assert "404" in parsed["error"]
    assert emission is None


def test_vlm_failure_returns_error_no_emission():
    from glados.vision.client import VisionClientError
    fake_disco = MagicMock()
    fake_disco.resolve_camera_name.return_value = "camera.backyard_high"
    with patch("glados.core.builtin_tools._get_camera_discovery", return_value=fake_disco), \
         patch("glados.core.builtin_tools.fetch_snapshot", return_value=b"\xff\xd8jpg"), \
         patch("glados.core.builtin_tools.describe_images",
               side_effect=VisionClientError("vision endpoint http://x failed: timeout")):
        result_json, emission = invoke_image_yielding_tool(
            "look_at_camera", {"camera_name": "back yard"}
        )
    parsed = json.loads(result_json)
    assert "error" in parsed
    assert "http://x" in parsed["error"]
    assert emission is None
