"""LLM & Services URL edits must sync into glados_config.yaml.

The LLM & Services page edits ``services.yaml`` via the pydantic
ServicesConfig model, but the chat engine's ``GladosConfig`` reads
``completion_url`` (for chat) and ``autonomy.completion_url`` from a
separate ``glados_config.yaml``. Without syncing, an operator who
retargets their LLM URL in the UI will continue to hit the old URL
on chat and autonomy LLM calls.

The system stores the bare ``scheme://host:port`` form everywhere;
protocol-internal paths (``/v1/chat/completions``, ``/v1/models``,
``/v1/audio/*``) are appended only at dispatch time so the operator
never has to type or know about them. ``_ollama_chat_url`` is the
forgiving normalizer that strips a stale path the operator might have
pasted from old docs.
"""
from __future__ import annotations

import pytest
import yaml

from glados.webui.tts_ui import _ollama_chat_url


class TestOllamaChatUrl:
    def test_bare_base_passes_through(self) -> None:
        assert _ollama_chat_url("http://ollama:11434") == "http://ollama:11434"

    def test_trailing_slash_stripped(self) -> None:
        assert _ollama_chat_url("http://ollama:11434/") == "http://ollama:11434"

    def test_chat_completions_path_stripped(self) -> None:
        assert (
            _ollama_chat_url("http://192.168.1.75:11434/v1/chat/completions")
            == "http://192.168.1.75:11434"
        )

    def test_api_chat_path_stripped(self) -> None:
        assert (
            _ollama_chat_url("http://192.168.1.75:11434/api/chat")
            == "http://192.168.1.75:11434"
        )

    def test_api_tags_path_stripped(self) -> None:
        # Operators who tested with /api/tags before pasting into the
        # URL field shouldn't have their config end up at /api/tags.
        assert (
            _ollama_chat_url("http://192.168.1.75:11434/api/tags")
            == "http://192.168.1.75:11434"
        )

    def test_v1_models_path_stripped(self) -> None:
        assert (
            _ollama_chat_url("http://192.168.1.75:11434/v1/models")
            == "http://192.168.1.75:11434"
        )

    def test_ipv4_host(self) -> None:
        assert _ollama_chat_url("http://10.0.0.10:11436") == "http://10.0.0.10:11436"

    def test_https_passes_through(self) -> None:
        assert (
            _ollama_chat_url("https://llm.example.com:443")
            == "https://llm.example.com:443"
        )

    def test_empty_url_returns_empty(self) -> None:
        # The sync layer skips empty URLs; we just need the helper not
        # to crash on them.
        assert _ollama_chat_url("") == ""

    def test_whitespace_only_returns_empty(self) -> None:
        assert _ollama_chat_url("   ") == ""


class TestSyncRewrites:
    """Simulate the YAML-rewrite behavior by reading/patching a dict
    the way ``_sync_glados_config_urls`` does. Avoids spinning up the
    HTTP handler."""

    def _fake_glados_config(self) -> dict:
        return {
            "Glados": {
                "llm_model": "qwen2.5:14b-instruct-q4_K_M",
                "completion_url": "http://10.0.0.10:11434",
                "autonomy": {
                    "enabled": True,
                    "completion_url": "http://10.0.0.10:11434",
                    "llm_model": "qwen2.5:14b-instruct-q4_K_M",
                },
            }
        }

    def _apply_sync(self, cfg: dict, services_payload: dict) -> dict:
        """Mirror the logic in _sync_glados_config_urls without the
        IO bits. Returns the mutated cfg for assertion."""
        glados = cfg.get("Glados") or {}
        interactive = ((services_payload.get("llm_interactive") or {}).get("url") or "").strip()
        autonomy = ((services_payload.get("llm_autonomy") or {}).get("url") or "").strip()
        if interactive:
            glados["completion_url"] = _ollama_chat_url(interactive)
        if autonomy:
            auton = glados.get("autonomy")
            if isinstance(auton, dict):
                auton["completion_url"] = _ollama_chat_url(autonomy)
        return cfg

    def test_interactive_change_rewrites_chat_url(self) -> None:
        cfg = self._fake_glados_config()
        self._apply_sync(cfg, {
            "llm_interactive": {"url": "http://10.0.0.10:11436"},
        })
        assert cfg["Glados"]["completion_url"] == "http://10.0.0.10:11436"

    def test_autonomy_change_rewrites_autonomy_url(self) -> None:
        cfg = self._fake_glados_config()
        self._apply_sync(cfg, {
            "llm_autonomy": {"url": "http://t4-host:11436"},
        })
        assert cfg["Glados"]["autonomy"]["completion_url"] == "http://t4-host:11436"

    def test_both_rewrite_independently(self) -> None:
        cfg = self._fake_glados_config()
        self._apply_sync(cfg, {
            "llm_interactive": {"url": "http://host-a:11434"},
            "llm_autonomy":    {"url": "http://host-b:11436"},
        })
        assert cfg["Glados"]["completion_url"] == "http://host-a:11434"
        assert cfg["Glados"]["autonomy"]["completion_url"] == "http://host-b:11436"

    def test_pasted_chat_path_is_stripped_on_save(self) -> None:
        """Operator pastes a full ``/v1/chat/completions`` URL; the
        normalizer must strip the path so storage stays bare."""
        cfg = self._fake_glados_config()
        self._apply_sync(cfg, {
            "llm_interactive": {"url": "http://host-a:11434/v1/chat/completions"},
        })
        assert cfg["Glados"]["completion_url"] == "http://host-a:11434"

    def test_empty_url_leaves_existing_alone(self) -> None:
        cfg = self._fake_glados_config()
        before = cfg["Glados"]["completion_url"]
        self._apply_sync(cfg, {"llm_interactive": {"url": ""}})
        assert cfg["Glados"]["completion_url"] == before

    def test_missing_service_keys_leave_config_alone(self) -> None:
        cfg = self._fake_glados_config()
        before_chat = cfg["Glados"]["completion_url"]
        before_auton = cfg["Glados"]["autonomy"]["completion_url"]
        # Payload with other unrelated services — no LLM keys.
        self._apply_sync(cfg, {"tts": {"url": "http://speaches:8800"}})
        assert cfg["Glados"]["completion_url"] == before_chat
        assert cfg["Glados"]["autonomy"]["completion_url"] == before_auton


def test_yaml_roundtrip_preserves_structure(tmp_path) -> None:
    """The sync must write valid YAML that parses back into a structure
    the engine can still use. Catches any silent loss of nested keys
    during read-modify-write."""
    p = tmp_path / "glados_config.yaml"
    p.write_text(yaml.safe_dump({
        "Glados": {
            "llm_model": "qwen2.5:14b-instruct-q4_K_M",
            "completion_url": "http://old:11434",
            "autonomy": {
                "enabled": True,
                "completion_url": "http://old:11434",
            },
            "voice": "glados",
        },
    }, sort_keys=False), encoding="utf-8")

    raw = yaml.safe_load(p.read_text())
    raw["Glados"]["completion_url"] = _ollama_chat_url("http://new:11436")
    raw["Glados"]["autonomy"]["completion_url"] = _ollama_chat_url("http://new:11436")
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    reloaded = yaml.safe_load(p.read_text())
    assert reloaded["Glados"]["completion_url"] == "http://new:11436"
    assert reloaded["Glados"]["autonomy"]["completion_url"] == "http://new:11436"
    # Unrelated fields survived.
    assert reloaded["Glados"]["voice"] == "glados"
    assert reloaded["Glados"]["autonomy"]["enabled"] is True
