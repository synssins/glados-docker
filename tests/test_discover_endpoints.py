"""Tests for Stage 3 Phase 5 service-discovery helpers.

These tests cover the module-level helpers (discover_ollama,
discover_voices, discover_health) in glados.webui.tts_ui. The helpers
are pure functions — they fetch JSON from an operator-supplied URL
and normalise the response shape — so they can be unit tested without
spinning up the HTTP handler.

Contract verified here:
  - Happy path: well-formed upstream response → 200 with normalised
    shape the UI dropdowns consume.
  - Unreachable URL → 502 with a short reason; never a 5xx stack
    trace.
  - Non-JSON response → 502 "invalid JSON response".
  - Empty / malformed URL → 400 (validation, not a network round trip).
  - Unexpected shape (missing "models" key) → 502, no crash.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from glados.webui.tts_ui import (
    discover_health,
    discover_ollama,
    discover_voices,
)


def _fake_response(body: bytes | dict, status: int = 200):
    """Context-manager mock for urlopen that yields an object with
    .read() and .status, matching what discover_* expects."""
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    status_code = status

    class _Resp:
        status = status_code

        def read(self):
            return payload

    class _Ctx:
        def __enter__(self_inner):
            return _Resp()

        def __exit__(self_inner, *a):
            return False
    return _Ctx()


class TestDiscoverOllama:
    def test_happy_path_returns_model_list(self) -> None:
        upstream = {
            "models": [
                {"name": "qwen2.5:14b-instruct-q4_K_M",
                 "size": 9123456789, "modified_at": "2026-04-01"},
                {"name": "qwen2.5:3b-instruct-q4_K_M",
                 "size": 1234567890, "modified_at": "2026-04-01"},
            ],
        }
        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   return_value=_fake_response(upstream)):
            status, payload = discover_ollama("http://192.168.1.75:11434")
        assert status == 200
        assert payload["count"] == 2
        assert payload["url"] == "http://192.168.1.75:11434"
        names = [m["name"] for m in payload["models"]]
        assert "qwen2.5:14b-instruct-q4_K_M" in names

    def test_unreachable_url_returns_502(self) -> None:
        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("Connection refused")):
            status, payload = discover_ollama("http://10.0.0.1:11434")
        assert status == 502
        assert "unreachable" in payload["error"]

    def test_non_json_response_returns_502(self) -> None:
        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   return_value=_fake_response(b"<html>404</html>")):
            status, payload = discover_ollama("http://192.168.1.75:11434")
        assert status == 502
        assert "invalid JSON" in payload["error"]

    def test_unexpected_shape_returns_502(self) -> None:
        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   return_value=_fake_response({"ok": True})):
            status, payload = discover_ollama("http://192.168.1.75:11434")
        assert status == 502

    def test_invalid_url_returns_400(self) -> None:
        status, payload = discover_ollama("")
        assert status == 400
        status, payload = discover_ollama("not-a-url")
        assert status == 400


class TestDiscoverVoices:
    def test_top_level_list_accepted(self) -> None:
        upstream = [
            {"voice_id": "glados", "model_id": "piper-glados"},
            {"voice_id": "en_US-amy-medium", "model_id": "piper"},
        ]
        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   return_value=_fake_response(upstream)):
            status, payload = discover_voices("http://192.168.1.75:5050")
        assert status == 200
        assert payload["count"] == 2
        assert payload["voices"][0]["name"] == "glados"

    def test_wrapped_data_accepted(self) -> None:
        """Some TTS deployments wrap the list in {"data": [...]}."""
        upstream = {"data": [{"id": "glados"}, {"id": "en_US-amy-medium"}]}
        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   return_value=_fake_response(upstream)):
            status, payload = discover_voices("http://192.168.1.75:5050")
        assert status == 200
        assert payload["count"] == 2

    def test_wrapped_voices_accepted(self) -> None:
        """GLaDOS Piper / custom TTS shapes wrap the list in
        {"voices": [...]} — often with string entries, not objects.
        Regression guard for the 2026-04-18 "unexpected response shape"
        bug surfaced on the production TTS Engine card."""
        upstream = {"voices": ["glados", "startrek-computer"]}
        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   return_value=_fake_response(upstream)):
            status, payload = discover_voices("http://192.168.1.75:5050")
        assert status == 200
        assert payload["count"] == 2
        names = [v["name"] for v in payload["voices"]]
        assert "glados" in names
        assert "startrek-computer" in names


class TestDiscoverHealth:
    def test_reachable_returns_ok_true(self) -> None:
        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   return_value=_fake_response({"status": "ok"}, status=200)):
            status, payload = discover_health("http://192.168.1.75:11434")
        assert status == 200
        assert payload["ok"] is True
        assert payload["status"] == 200
        assert "latency_ms" in payload

    def test_unreachable_returns_ok_false_not_5xx(self) -> None:
        """Health checks must never return 5xx — operators poll this
        every 30 s; a red dot in the UI is the correct signal, not a
        server error."""
        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("refused")):
            status, payload = discover_health("http://10.0.0.1:11434")
        assert status == 200
        assert payload["ok"] is False
        assert payload["status"] is None

    def test_kind_ollama_probes_api_tags(self) -> None:
        """Ollama has no /health; probing kind=ollama must hit /api/tags
        and get a 200 back. Regression guard for the 2026-04-19 all-red
        services-grid bug."""
        calls = []

        def _capture_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return _fake_response({"models": []}, status=200)

        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   side_effect=_capture_urlopen):
            status, payload = discover_health(
                "http://192.168.1.75:11436", kind="ollama",
            )
        assert status == 200
        assert payload["ok"] is True
        assert any(url.endswith("/api/tags") for url in calls), (
            f"expected /api/tags to be probed, got {calls!r}"
        )

    def test_kind_speaches_probes_v1_voices(self) -> None:
        calls = []

        def _capture_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return _fake_response({"voices": []}, status=200)

        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   side_effect=_capture_urlopen):
            status, payload = discover_health(
                "http://192.168.1.75:5050", kind="speaches",
            )
        assert status == 200
        assert payload["ok"] is True
        assert any(url.endswith("/v1/voices") for url in calls), (
            f"expected /v1/voices to be probed, got {calls!r}"
        )

    def test_unknown_kind_falls_through_probe_paths(self) -> None:
        """When the caller doesn't supply a kind hint, the probe tries
        /api/tags first, then /v1/voices, then /health, etc. Simulate
        an Ollama-like service that answers 404 on /api/tags (odd)
        and 200 on /v1/voices — the probe should eventually succeed."""
        def _responses(req, timeout=None):
            path = req.full_url.rsplit("/", 1)[-1]
            if "tags" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 404, "Not Found", hdrs=None, fp=None,
                )
            return _fake_response({"voices": []}, status=200)

        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   side_effect=_responses):
            status, payload = discover_health("http://host")
        assert status == 200
        assert payload["ok"] is True

    def test_connection_refused_short_circuits(self) -> None:
        """URLError (connection refused / DNS failure) means the service
        isn't answering at all — don't keep probing additional paths."""
        calls = []

        def _refuse(req, timeout=None):
            calls.append(req.full_url)
            raise urllib.error.URLError("connection refused")

        with patch("glados.webui.tts_ui.urllib.request.urlopen",
                   side_effect=_refuse):
            status, payload = discover_health("http://dead-host:11435")
        assert status == 200
        assert payload["ok"] is False
        assert payload["reason"] == "connection refused"
        assert len(calls) == 1, (
            f"expected a single probe on connection refused, got {len(calls)}"
        )
