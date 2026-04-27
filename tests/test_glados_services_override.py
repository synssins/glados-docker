"""Phase 8.13 — load-time reconciliation of Glados block with services.yaml.

The LLM & Services WebUI page writes to `services.yaml`; the legacy
`Glados` block in `glados_config.yaml` duplicates `llm_model` /
`completion_url` for engine convenience. A save-side sync keeps them
aligned on UI writes (`tts_ui._sync_glados_config_urls`), but a hand
edit or a backup restore can still leave the Glados block stale. At
load time, services must override Glados whenever they disagree so
there is no path where the UI displays one model while the engine
runs another.

These tests exercise `_reconcile_glados_with_services` directly — the
pure-dict rewriter that runs inside `GladosConfig.from_yaml` before
pydantic validation.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
import yaml
from loguru import logger

from glados.core import engine as engine_mod
from glados.core.config_store import cfg


@contextmanager
def _capture_warnings() -> Iterator[list[str]]:
    messages: list[str] = []
    handler_id = logger.add(
        lambda msg: messages.append(str(msg)),
        level="WARNING",
        format="{message}",
    )
    try:
        yield messages
    finally:
        logger.remove(handler_id)


@pytest.fixture
def configs_dir(tmp_path: Path) -> Iterator[Path]:
    """Point the cfg singleton at a clean configs dir for the test and
    restore it afterwards. Tests opt in by writing their own services.yaml
    into the yielded dir."""
    d = tmp_path / "configs"
    d.mkdir()
    original_dir = cfg._configs_dir
    original_loaded = cfg._loaded
    cfg._configs_dir = d
    cfg._loaded = False
    try:
        yield d
    finally:
        cfg._configs_dir = original_dir
        cfg._loaded = False


def _write_services(configs_dir: Path, payload: dict) -> None:
    (configs_dir / "services.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )


def _full_glados_raw() -> dict:
    return {
        "llm_model": "qwen3:8b",
        "completion_url": "http://10.0.0.10:11434/api/chat",
        "autonomy": {
            "enabled": True,
            "llm_model": "qwen3:8b",
            "completion_url": "http://10.0.0.10:11434/api/chat",
        },
    }


class TestOllamaAsChatUrl:
    def test_bare_base_gets_api_chat_suffix(self) -> None:
        assert engine_mod._ollama_as_chat_url("http://ollama:11434") == "http://ollama:11434/api/chat"

    def test_trailing_slash_stripped(self) -> None:
        assert engine_mod._ollama_as_chat_url("http://ollama:11434/") == "http://ollama:11434/api/chat"

    def test_already_chat_path_unchanged(self) -> None:
        assert engine_mod._ollama_as_chat_url("http://ollama:11434/api/chat") == "http://ollama:11434/api/chat"

    def test_other_api_path_rewritten(self) -> None:
        assert engine_mod._ollama_as_chat_url("http://ollama:11434/api/tags") == "http://ollama:11434/api/chat"

    def test_empty_returns_empty(self) -> None:
        assert engine_mod._ollama_as_chat_url("") == ""
        assert engine_mod._ollama_as_chat_url(None) == ""

    def test_openai_chat_completions_url_passes_through(self) -> None:
        """If services.yaml stores an OpenAI-compatible chat URL
        (LM Studio, vLLM, mainline llama.cpp), the reconciler must NOT
        append ``/api/chat`` and produce the malformed
        ``/v1/chat/completions/api/chat``. Operator-mandate Item #3 —
        the engine speaks OpenAI when the URL says OpenAI."""
        assert (
            engine_mod._ollama_as_chat_url("http://lmstudio:11434/v1/chat/completions")
            == "http://lmstudio:11434/v1/chat/completions"
        )

    def test_openai_chat_completions_trailing_slash_passes_through(self) -> None:
        assert (
            engine_mod._ollama_as_chat_url("http://lmstudio:11434/v1/chat/completions/")
            == "http://lmstudio:11434/v1/chat/completions"
        )


class TestReconcileOverrides:
    def test_model_override_fires_when_services_disagrees(self, configs_dir: Path) -> None:
        _write_services(configs_dir, {
            "ollama_interactive": {
                "url": "http://10.0.0.10:11434",
                "model": "qwen3:14b",
            },
            "ollama_autonomy": {
                "url": "http://10.0.0.10:11434",
                "model": "qwen3:14b",
            },
        })
        raw = _full_glados_raw()
        with _capture_warnings() as warnings:
            out = engine_mod._reconcile_glados_with_services(raw)
        assert out["llm_model"] == "qwen3:14b"
        assert out["autonomy"]["llm_model"] == "qwen3:14b"
        assert any("Glados.llm_model" in m and "qwen3:14b" in m for m in warnings)
        assert any("Glados.autonomy.llm_model" in m and "qwen3:14b" in m for m in warnings)

    def test_completion_url_override_fires_when_services_disagrees(self, configs_dir: Path) -> None:
        _write_services(configs_dir, {
            "ollama_interactive": {"url": "http://10.0.0.10:11436"},
            "ollama_autonomy":    {"url": "http://10.0.0.10:11436"},
        })
        raw = _full_glados_raw()
        with _capture_warnings() as warnings:
            out = engine_mod._reconcile_glados_with_services(raw)
        assert out["completion_url"] == "http://10.0.0.10:11436/api/chat"
        assert out["autonomy"]["completion_url"] == "http://10.0.0.10:11436/api/chat"
        assert any("Glados.completion_url" in m for m in warnings)
        assert any("Glados.autonomy.completion_url" in m for m in warnings)

    def test_no_override_when_services_match_glados(self, configs_dir: Path) -> None:
        _write_services(configs_dir, {
            "ollama_interactive": {
                "url": "http://10.0.0.10:11434",
                "model": "qwen3:8b",
            },
            "ollama_autonomy": {
                "url": "http://10.0.0.10:11434",
                "model": "qwen3:8b",
            },
        })
        raw = _full_glados_raw()
        with _capture_warnings() as warnings:
            out = engine_mod._reconcile_glados_with_services(raw)
        assert out["llm_model"] == "qwen3:8b"
        assert out["completion_url"] == "http://10.0.0.10:11434/api/chat"
        assert out["autonomy"]["llm_model"] == "qwen3:8b"
        assert out["autonomy"]["completion_url"] == "http://10.0.0.10:11434/api/chat"
        assert not any("overridden" in m for m in warnings)

    def test_empty_services_model_does_not_blank_glados(self, configs_dir: Path) -> None:
        """A half-configured services.yaml with URL but no model must not
        overwrite a working llm_model with an empty string."""
        _write_services(configs_dir, {
            "ollama_interactive": {"url": "http://10.0.0.10:11434"},
        })
        raw = _full_glados_raw()
        out = engine_mod._reconcile_glados_with_services(raw)
        assert out["llm_model"] == "qwen3:8b"
        assert out["autonomy"]["llm_model"] == "qwen3:8b"

    def test_services_yaml_absent_is_a_noop(self, configs_dir: Path) -> None:
        """Dev / test runs without a services.yaml file must not trigger
        reconciliation at all — otherwise pydantic ServicesConfig defaults
        would pretend to be operator-authoritative."""
        raw = _full_glados_raw()
        before = yaml.safe_dump(raw, sort_keys=True)
        with _capture_warnings() as warnings:
            out = engine_mod._reconcile_glados_with_services(raw)
        after = yaml.safe_dump(out, sort_keys=True)
        assert before == after
        assert not any("overridden" in m for m in warnings)

    def test_non_dict_input_is_a_noop(self) -> None:
        assert engine_mod._reconcile_glados_with_services(None) is None
        assert engine_mod._reconcile_glados_with_services("not a dict") == "not a dict"

    def test_missing_autonomy_block_tolerated(self, configs_dir: Path) -> None:
        _write_services(configs_dir, {
            "ollama_interactive": {"url": "http://host:11434", "model": "qwen3:14b"},
            "ollama_autonomy":    {"url": "http://host:11434", "model": "qwen3:14b"},
        })
        raw = {
            "llm_model": "qwen3:8b",
            "completion_url": "http://host:11434/api/chat",
        }
        out = engine_mod._reconcile_glados_with_services(raw)
        assert out["llm_model"] == "qwen3:14b"
        assert "autonomy" not in out

    def test_bare_url_matches_chat_form_in_glados(self, configs_dir: Path) -> None:
        """services.yaml stores the bare base; Glados stores /api/chat.
        When they point at the same host they must compare equal — no
        spurious override / warning."""
        _write_services(configs_dir, {
            "ollama_interactive": {"url": "http://10.0.0.10:11434"},
            "ollama_autonomy":    {"url": "http://10.0.0.10:11434"},
        })
        raw = {
            "llm_model": "qwen3:8b",
            "completion_url": "http://10.0.0.10:11434/api/chat",
            "autonomy": {
                "enabled": True,
                "completion_url": "http://10.0.0.10:11434/api/chat",
            },
        }
        with _capture_warnings() as warnings:
            out = engine_mod._reconcile_glados_with_services(raw)
        assert out["completion_url"] == "http://10.0.0.10:11434/api/chat"
        assert not any("completion_url" in m and "overridden" in m for m in warnings)
