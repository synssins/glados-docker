"""LLMConfig.for_slot resolves a slot name to URL+model from cfg.services."""

from __future__ import annotations

import pytest

from glados.autonomy.llm_client import LLMConfig


class TestLLMConfigForSlot:
    def test_resolves_llm_triage(self) -> None:
        cfg = LLMConfig.for_slot("llm_triage")
        # Triage slot has a hardcoded default model (Task 0); verify it
        # resolves through, and that the URL+model match cfg.services.
        assert cfg.model  # default is llama-3.2-1b-instruct
        assert cfg.url.startswith("http")
        from glados.core.config_store import cfg as store
        assert cfg.url == store.services.llm_triage.url
        assert cfg.model == store.services.llm_triage.model

    def test_resolves_llm_interactive(self) -> None:
        # llm_interactive has no hardcoded default model — operators
        # supply it via services.yaml. Resolver must propagate whatever
        # cfg.services.llm_interactive holds (model may be None when
        # nothing is configured).
        cfg = LLMConfig.for_slot("llm_interactive")
        assert cfg.url.startswith("http")
        from glados.core.config_store import cfg as store
        assert cfg.url == store.services.llm_interactive.url
        assert cfg.model == store.services.llm_interactive.model

    def test_resolves_llm_autonomy(self) -> None:
        cfg = LLMConfig.for_slot("llm_autonomy")
        from glados.core.config_store import cfg as store
        assert cfg.url == store.services.llm_autonomy.url
        assert cfg.model == store.services.llm_autonomy.model

    def test_unknown_slot_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            LLMConfig.for_slot("not_a_slot")
        assert "not_a_slot" in str(exc.value)

    def test_passes_through_timeout_kwarg(self) -> None:
        cfg = LLMConfig.for_slot("llm_triage", timeout=5.0)
        assert cfg.timeout == 5.0
