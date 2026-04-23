"""Stage 3 Phase A: lock-in tests for personality + model-options.

These tests guard the contract that container-side persona injection
remains the sole source of GLaDOS personality (the `glados:latest`
Modelfile is being retired). Without these, a future change could
silently revert temperature/top_p/num_ctx to hardcoded values and the
neutral-model deployment would lose its persona-strength tuning.

The chat path itself (_stream_chat_sse) is exercised by integration
tests against a live Ollama; here we lock the small surface area that
DOES have a unit-test home: the model_options dataclass and its
env-overrides-YAML behavior.
"""

from __future__ import annotations

import os

import pytest

from glados.core.config_store import ModelOptionsConfig, PersonalityConfig


class TestModelOptionsConfig:
    def test_defaults_match_neutral_base_model_recommended_settings(self) -> None:
        """Defaults are tuned for qwen2.5:14b-instruct without a Modelfile.
        If we change them, do it deliberately — these values were chosen
        after observing persona strength against a neutral base."""
        opts = ModelOptionsConfig()
        assert opts.temperature == 0.7
        assert opts.top_p == 0.9
        assert opts.num_ctx == 16384      # fits persona + 21 MCP tools
        assert opts.repeat_penalty == 1.1

    def test_to_ollama_options_returns_complete_dict(self) -> None:
        """Lock the exact key set sent to Ollama. Adding a key here
        without updating the api_wrapper consumer would silently drop it."""
        opts = ModelOptionsConfig()
        d = opts.to_ollama_options()
        assert set(d.keys()) == {"temperature", "top_p", "num_ctx", "repeat_penalty"}

    def test_yaml_values_apply_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clear any operator env so the YAML defaults stand.
        for k in ("OLLAMA_TEMPERATURE", "OLLAMA_TOP_P",
                  "OLLAMA_NUM_CTX", "OLLAMA_REPEAT_PENALTY"):
            monkeypatch.delenv(k, raising=False)
        opts = ModelOptionsConfig(temperature=0.3, num_ctx=8192)
        assert opts.temperature == 0.3
        assert opts.num_ctx == 8192

    def test_yaml_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Policy change 2026-04-23: YAML (WebUI Save) is authoritative
        for every operator-facing field. Env values only seed pydantic
        defaults when YAML is missing the field entirely. Same
        reasoning as HomeAssistantGlobal — prevents a stale
        OLLAMA_TEMPERATURE in compose from silently reverting
        WebUI-saved tuning on the next engine reload."""
        monkeypatch.setenv("OLLAMA_TEMPERATURE", "0.4")
        monkeypatch.setenv("OLLAMA_TOP_P", "0.8")
        monkeypatch.setenv("OLLAMA_NUM_CTX", "32768")
        monkeypatch.setenv("OLLAMA_REPEAT_PENALTY", "1.05")
        # YAML values MUST win over env now.
        opts = ModelOptionsConfig(temperature=0.99, top_p=0.99,
                                   num_ctx=1, repeat_penalty=2.0)
        assert opts.temperature == 0.99
        assert opts.top_p == 0.99
        assert opts.num_ctx == 1
        assert opts.repeat_penalty == 2.0

    def test_env_seeds_when_yaml_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Initial-install contract: empty YAML means env values fill
        in the defaults so the container boots with sensible values."""
        monkeypatch.setenv("OLLAMA_TEMPERATURE", "0.4")
        monkeypatch.setenv("OLLAMA_TOP_P", "0.8")
        monkeypatch.setenv("OLLAMA_NUM_CTX", "32768")
        monkeypatch.setenv("OLLAMA_REPEAT_PENALTY", "1.05")
        opts = ModelOptionsConfig()
        assert opts.temperature == 0.4
        assert opts.top_p == 0.8
        assert opts.num_ctx == 32768
        assert opts.repeat_penalty == 1.05

    def test_invalid_env_falls_back_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A garbled OLLAMA_TEMPERATURE must not crash startup.
        Defensive: operators set env in many ways and a bad value
        shouldn't take the engine down."""
        monkeypatch.setenv("OLLAMA_TEMPERATURE", "not-a-number")
        monkeypatch.setenv("OLLAMA_NUM_CTX", "")  # empty stays default
        opts = ModelOptionsConfig()  # uses dataclass defaults
        assert opts.temperature == 0.7    # YAML default preserved
        assert opts.num_ctx == 16384

    def test_partial_env_overrides_only_specified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Setting only OLLAMA_TEMPERATURE doesn't disturb the others."""
        for k in ("OLLAMA_TOP_P", "OLLAMA_NUM_CTX", "OLLAMA_REPEAT_PENALTY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OLLAMA_TEMPERATURE", "0.2")
        opts = ModelOptionsConfig()
        assert opts.temperature == 0.2
        assert opts.top_p == 0.9          # default
        assert opts.num_ctx == 16384      # default


class TestPersonalityConfigContract:
    def test_personality_config_includes_model_options(self) -> None:
        """PersonalityConfig must expose model_options as a top-level
        attribute. The api_wrapper's `cfg.personality.model_options`
        access depends on this."""
        p = PersonalityConfig()
        assert isinstance(p.model_options, ModelOptionsConfig)

    def test_model_options_can_be_overridden_via_init(self) -> None:
        """Operators may want to ship a personality.yaml that locks
        specific options. Make sure the model_options nested config
        accepts a dict on construction (Pydantic v2 behavior)."""
        p = PersonalityConfig.model_validate({
            "model_options": {"temperature": 0.5, "num_ctx": 8192},
        })
        assert p.model_options.temperature == 0.5
        assert p.model_options.num_ctx == 8192
        # Other fields keep defaults.
        assert p.model_options.top_p == 0.9
