"""Unit tests for glados/vision/client.py — VLM client."""

from __future__ import annotations

import base64
import json
from unittest.mock import patch, MagicMock

import pytest

from glados.vision.client import describe_images, VisionClientError


@pytest.fixture
def fake_jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def _ok_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


def _err_response(status: int, body: str = "boom") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    return resp


def _stub_slot(url: str = "http://aibox:11437", model: str = "qwen-vl", api_key: str | None = None):
    return MagicMock(url=url, model=model, api_key=api_key)


def test_single_image_returns_description(fake_jpeg):
    with patch("glados.vision.client._get_slot", return_value=_stub_slot()), \
         patch("glados.vision.client.requests.post", return_value=_ok_response("a cat")) as post:
        result = describe_images([fake_jpeg], "what is in this image?")
    assert result == "a cat"
    args, kwargs = post.call_args
    body = kwargs["json"]
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert parts[-1] == {"type": "text", "text": "what is in this image?"}
    assert body["model"] == "qwen-vl"


def test_multi_image_preserves_order(fake_jpeg):
    img2 = b"\xff\xd8\xff\xe0two"
    with patch("glados.vision.client._get_slot", return_value=_stub_slot()), \
         patch("glados.vision.client.requests.post", return_value=_ok_response("two things")) as post:
        describe_images([fake_jpeg, img2], "compare")
        last_call = post.call_args
    parts = last_call.kwargs["json"]["messages"][0]["content"]
    image_parts = [p for p in parts if p["type"] == "image_url"]
    assert len(image_parts) == 2
    assert base64.b64decode(image_parts[0]["image_url"]["url"].split(",", 1)[1]) == fake_jpeg
    assert base64.b64decode(image_parts[1]["image_url"]["url"].split(",", 1)[1]) == img2


def test_api_key_passed_in_authorization(fake_jpeg):
    with patch("glados.vision.client._get_slot", return_value=_stub_slot(api_key="secret")), \
         patch("glados.vision.client.requests.post", return_value=_ok_response("ok")) as post:
        describe_images([fake_jpeg], "x")
    headers = post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer secret"


def test_no_api_key_no_authorization(fake_jpeg):
    with patch("glados.vision.client._get_slot", return_value=_stub_slot(api_key=None)), \
         patch("glados.vision.client.requests.post", return_value=_ok_response("ok")) as post:
        describe_images([fake_jpeg], "x")
    headers = post.call_args.kwargs["headers"]
    assert "Authorization" not in headers


def test_non_200_raises(fake_jpeg):
    with patch("glados.vision.client._get_slot", return_value=_stub_slot()), \
         patch("glados.vision.client.requests.post", return_value=_err_response(502, "upstream gone")):
        with pytest.raises(VisionClientError) as exc:
            describe_images([fake_jpeg], "x")
    assert "502" in str(exc.value)
    assert "upstream gone" in str(exc.value)


def test_timeout_raises(fake_jpeg):
    import requests
    with patch("glados.vision.client._get_slot", return_value=_stub_slot()), \
         patch("glados.vision.client.requests.post", side_effect=requests.Timeout("slow")):
        with pytest.raises(VisionClientError) as exc:
            describe_images([fake_jpeg], "x")
    assert "http://aibox:11437" in str(exc.value)


def test_empty_image_list_raises(fake_jpeg):
    with pytest.raises(ValueError):
        describe_images([], "x")
