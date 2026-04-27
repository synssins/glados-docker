"""LLM & Services URL edits must sync into glados_config.yaml.

Phase 6's LLM & Services page edits `services.yaml` via the pydantic
ServicesConfig model, but the chat engine's `GladosConfig` reads
`completion_url` (for chat) and `autonomy.completion_url` from a
separate `glados_config.yaml`. Without syncing, an operator who
retargets their Ollama URL in the UI will continue to hit the old
URL on chat and autonomy LLM calls — surfacing as a 504 Gateway
Timeout the first time they try to chat after a server move
(2026-04-19 operator report).

The sync layer lives inside `_put_config_section` on the handler
and is invoked whenever the services section is saved. These tests
cover the two lower-level pure functions exposed at module scope:
`_ollama_chat_url` and the URL-rewrite contract that drives the
sync.
"""
from __future__ import annotations

import pytest
import yaml

from glados.webui.tts_ui import _ollama_chat_url


class TestOllamaChatUrl:
    def test_bare_base_url_gets_api_chat_suffix(self) -> None:
        assert _ollama_chat_url("http://ollama:11434") == "http://ollama:11434/api/chat"

    def test_trailing_slash_stripped(self) -> None:
        assert _ollama_chat_url("http://ollama:11434/") == "http://ollama:11434/api/chat"

    def test_already_chat_path_unchanged(self) -> None:
        assert _ollama_chat_url("http://ollama:11434/api/chat") == "http://ollama:11434/api/chat"

    def test_other_api_path_rewritten(self) -> None:
        # Operators who tested with /api/tags before pasting into the
        # URL field shouldn't have their chat path end up at /api/tags.
        assert _ollama_chat_url("http://ollama:11434/api/tags") == "http://ollama:11434/api/chat"

    def test_ipv4_host(self) -> None:
        assert _ollama_chat_url("http://10.0.0.10:11436") == "http://10.0.0.10:11436/api/chat"

    def test_empty_url_returns_empty(self) -> None:
        # The sync layer skips empty URLs; we just need the helper not
        # to crash on them.
        assert _ollama_chat_url("") == ""

    def test_whitespace_only_returns_empty(self) -> None:
        assert _ollama_chat_url("   ") == ""

    def test_openai_chat_completions_url_passes_through(self) -> None:
        """OpenAI-compatible chat URL (LM Studio, vLLM, llama.cpp) must
        pass through unchanged. The sync would otherwise mangle it to
        ``/v1/chat/completions/api/chat`` — the live chat-blocker
        observed 2026-04-27."""
        assert (
            _ollama_chat_url("http://lmstudio:11434/v1/chat/completions")
            == "http://lmstudio:11434/v1/chat/completions"
        )

    def test_openai_chat_completions_trailing_slash_passes_through(self) -> None:
        assert (
            _ollama_chat_url("http://lmstudio:11434/v1/chat/completions/")
            == "http://lmstudio:11434/v1/chat/completions"
        )


class TestSyncRewrites:
    """Simulate the YAML-rewrite behavior by reading/patching a dict
    the way `_sync_glados_config_urls` does. Avoids spinning up the
    HTTP handler."""

    def _fake_glados_config(self) -> dict:
        return {
            "Glados": {
                "llm_model": "qwen2.5:14b-instruct-q4_K_M",
                "completion_url": "http://10.0.0.10:11434/api/chat",
                "autonomy": {
                    "enabled": True,
                    "completion_url": "http://10.0.0.10:11434/api/chat",
                    "llm_model": "qwen2.5:14b-instruct-q4_K_M",
                },
            }
        }

    def _apply_sync(self, cfg: dict, services_payload: dict) -> dict:
        """Mirror the logic in _sync_glados_config_urls without the
        IO bits. Returns the mutated cfg for assertion."""
        glados = cfg.get("Glados") or {}
        interactive = ((services_payload.get("ollama_interactive") or {}).get("url") or "").strip()
        autonomy = ((services_payload.get("ollama_autonomy") or {}).get("url") or "").strip()
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
            "ollama_interactive": {"url": "http://10.0.0.10:11436"},
        })
        assert cfg["Glados"]["completion_url"] == "http://10.0.0.10:11436/api/chat"

    def test_autonomy_change_rewrites_autonomy_url(self) -> None:
        cfg = self._fake_glados_config()
        self._apply_sync(cfg, {
            "ollama_autonomy": {"url": "http://t4-host:11436"},
        })
        assert cfg["Glados"]["autonomy"]["completion_url"] == "http://t4-host:11436/api/chat"

    def test_both_rewrite_independently(self) -> None:
        cfg = self._fake_glados_config()
        self._apply_sync(cfg, {
            "ollama_interactive": {"url": "http://host-a:11434"},
            "ollama_autonomy":    {"url": "http://host-b:11436"},
        })
        assert cfg["Glados"]["completion_url"] == "http://host-a:11434/api/chat"
        assert cfg["Glados"]["autonomy"]["completion_url"] == "http://host-b:11436/api/chat"

    def test_empty_url_leaves_existing_alone(self) -> None:
        cfg = self._fake_glados_config()
        before = cfg["Glados"]["completion_url"]
        self._apply_sync(cfg, {"ollama_interactive": {"url": ""}})
        assert cfg["Glados"]["completion_url"] == before

    def test_missing_service_keys_leave_config_alone(self) -> None:
        cfg = self._fake_glados_config()
        before_chat = cfg["Glados"]["completion_url"]
        before_auton = cfg["Glados"]["autonomy"]["completion_url"]
        # Payload with other unrelated services — no Ollama keys.
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
            "completion_url": "http://old:11434/api/chat",
            "autonomy": {
                "enabled": True,
                "completion_url": "http://old:11434/api/chat",
            },
            "voice": "glados",
        },
    }, sort_keys=False), encoding="utf-8")

    raw = yaml.safe_load(p.read_text())
    raw["Glados"]["completion_url"] = _ollama_chat_url("http://new:11436")
    raw["Glados"]["autonomy"]["completion_url"] = _ollama_chat_url("http://new:11436")
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    reloaded = yaml.safe_load(p.read_text())
    assert reloaded["Glados"]["completion_url"] == "http://new:11436/api/chat"
    assert reloaded["Glados"]["autonomy"]["completion_url"] == "http://new:11436/api/chat"
    # Unrelated fields survived.
    assert reloaded["Glados"]["voice"] == "glados"
    assert reloaded["Glados"]["autonomy"]["enabled"] is True
