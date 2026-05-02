# Autonomy Triage Split — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the autonomy lane from sending oversized prompts that overflow LM Studio's context window, crash the model, and starve the chat lane of slots. Split autonomy work into two lanes: classification/summarization on a small fast triage model with a size-capped prompt, utterance generation on the chat-quality model with full persona. Concurrent OpenAI-compliance cleanup: rename `services.yaml` slots from `ollama_*` to `llm_*` with one-release dual-key support.

**Architecture:** Add a fourth service slot `llm_triage` to `ServicesConfig`. Add `LLMConfig.for_slot(slot)` so callers don't hardcode endpoints. Introduce `MAX_AUTONOMY_USER_PROMPT_TOKENS` budget enforced inside `glados.autonomy.llm_client.llm_call` — when the user_prompt exceeds the cap, truncate the oldest content with a sentinel and emit a WARNING log. Route classification-only callers (Message Compaction, Memory classifier yes/no, Behavior Observer triage) through `llm_triage`; utterance-emitting subagents (Weather summary, proactive announcements) stay on `llm_autonomy` with persona. Schema rename ships in the same plan because every affected file already touches `services.ollama_*` references — doing the rename concurrently means callers reference the final names.

**Tech Stack:** Python 3.12 (stdlib `http.server` middleware), Pydantic v2 with field aliases for dual-key support, pytest 8.x, LM Studio OpenAI-compatible HTTP API at `aibox.local:11434`, Llama-3.2-1B-Instruct (already pre-pulled into LM Studio's index, ~1.32 GB Q4_K_M).

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `glados/core/config_store.py:332-365` | `ServicesConfig` + `ServiceEndpoint` definitions; add `llm_*` fields with `ollama_*` aliases for read-back compatibility | Modify |
| `glados/core/config_store.py:707` | `service_model()` helper docstring updated to reference new names | Modify |
| `glados/core/engine.py:149-230` | `_reconcile_glados_with_services` reads `svcs.llm_interactive` / `svcs.llm_autonomy` (not `ollama_*`) | Modify |
| `glados/webui/tts_ui.py:76` | `_default_chat_model()` reads `svcs.llm_interactive.model` | Modify |
| `glados/webui/tts_ui.py:4565-4610` | `_sync_glados_config_urls` and surrounding doc comments use new field names | Modify |
| `glados/webui/static/ui.js` | LLM & Services tab card labels + new triage card | Modify |
| `glados/autonomy/llm_client.py` | Add `LLMConfig.for_slot(slot)` classmethod; add `_truncate_user_prompt` budget enforcement; existing `llm_call` calls truncator before POST | Modify |
| `glados/autonomy/summarization.py` | `compact_messages` and `extract_facts` resolve config via `LLMConfig.for_slot("llm_triage")` instead of `cfg.service_model("ollama_autonomy")` | Modify |
| `glados/core/memory_writer.py:354,374` | Memory classifier and extractor use `LLMConfig.for_slot("llm_triage")` | Modify |
| `glados/intent/disambiguator.py:1974-1977` | Tier 2 disambiguator already uses `apply_model_family_directives`; verify `LLMConfig` resolution path also routes to `llm_triage` | Modify |
| `tests/test_config_defaults.py` | Add tests covering `llm_*` field load, `ollama_*` legacy alias parse, and dual-key precedence | Modify |
| `tests/test_glados_services_override.py` | Update existing references from `ollama_interactive` → `llm_interactive` | Modify |
| `tests/test_glados_config_url_sync.py` | Same field-rename update | Modify |
| `tests/test_autonomy_llm_client_budget.py` | New — covers `_truncate_user_prompt` and the integrated budget check in `llm_call` | New |
| `tests/test_llm_config_for_slot.py` | New — covers `LLMConfig.for_slot()` resolution for each known slot + unknown-slot error | New |
| `tests/test_summarization_uses_triage_slot.py` | New — patches `requests.post`, verifies summarization sends to the triage URL | New |
| `tests/test_memory_writer_uses_triage_slot.py` | New — same shape as above for memory_writer | New |
| `docs/superpowers/plans/2026-04-28-autonomy-triage-split.md.tasks.json` | Companion task persistence file | New |
| `docs/CHANGES.md` | New change entry documenting the rename + triage split | Modify |
| `C:\src\SESSION_STATE.md` | Top-section refresh after live verification | Modify |

---

## Task 0: Add `llm_triage` slot to `ServicesConfig` with `ollama_*` legacy aliases

**Goal:** Schema-level rename from `ollama_*` to `llm_*` lands first so subsequent tasks reference final names. Add a fourth slot `llm_triage`. Pydantic field aliases let operators keep `ollama_*` keys in their on-disk `services.yaml` for one release; on next save the file is rewritten with the new names.

**Files:**
- Modify: `glados/core/config_store.py:332-365`
- Modify: `glados/core/config_store.py:707` (docstring only)
- Modify: `tests/test_config_defaults.py` (add coverage)

**Acceptance Criteria:**
- [ ] `ServicesConfig` exposes `llm_interactive`, `llm_autonomy`, `llm_vision`, `llm_triage` fields with `ServiceEndpoint` defaults.
- [ ] Each field declares an alias so YAML files containing `ollama_interactive` / `ollama_autonomy` / `ollama_vision` still parse cleanly.
- [ ] After parse, `cfg.services.llm_interactive` is the authoritative attribute; `ollama_interactive` is NOT exposed (callers must use the new name).
- [ ] `llm_triage.model` defaults to `"llama-3.2-1b-instruct"`; URL defaults to the same Ollama base used by other slots (operator overrides via WebUI).
- [ ] On save, the YAML round-trip emits new key names (no `ollama_*` keys retained).
- [ ] New tests pass: `test_config_loads_legacy_ollama_keys`, `test_config_loads_new_llm_keys`, `test_config_save_writes_llm_keys`, `test_llm_triage_default`.
- [ ] Full suite remains green.

**Verify:** `python -m pytest tests/test_config_defaults.py -v` → all new tests PASS, no existing-test regressions.

**Steps:**

- [ ] **Step 1: Inspect the existing `ServicesConfig` block to lock in the surrounding patterns.**

```bash
grep -n -A 3 "ollama_interactive\|ollama_autonomy\|ollama_vision\|class ServicesConfig" glados/core/config_store.py | head -40
```

Expected: shows the three current fields plus the class declaration. Note the exact `ServiceEndpoint(...)` defaults so the new aliased fields keep them.

- [ ] **Step 2: Write the failing test for legacy-key compatibility.**

```python
# tests/test_config_defaults.py — add to the bottom

def test_config_loads_legacy_ollama_keys(tmp_path, monkeypatch):
    """Operators with services.yaml from before the rename must keep working."""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "services.yaml").write_text(
        "ollama_interactive:\n"
        "  url: http://example:11434/v1/chat/completions\n"
        "  model: qwen3-30b-a3b\n"
        "ollama_autonomy:\n"
        "  url: http://example:11434/v1/chat/completions\n"
        "  model: qwen3-30b-a3b\n"
        "ollama_vision:\n"
        "  url: http://example:11434/v1/chat/completions\n"
        "  model: qwen2.5-vl-3b-instruct\n"
    )
    monkeypatch.setenv("GLADOS_CONFIGS", str(cfgs))
    from glados.core import config_store
    config_store.cfg.reload()
    assert config_store.cfg.services.llm_interactive.model == "qwen3-30b-a3b"
    assert config_store.cfg.services.llm_autonomy.model == "qwen3-30b-a3b"
    assert config_store.cfg.services.llm_vision.model == "qwen2.5-vl-3b-instruct"


def test_config_loads_new_llm_keys(tmp_path, monkeypatch):
    """New on-disk shape with llm_* keys parses identically."""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "services.yaml").write_text(
        "llm_interactive:\n"
        "  url: http://example:11434/v1/chat/completions\n"
        "  model: qwen3-30b-a3b\n"
        "llm_triage:\n"
        "  url: http://example:11434/v1/chat/completions\n"
        "  model: llama-3.2-1b-instruct\n"
    )
    monkeypatch.setenv("GLADOS_CONFIGS", str(cfgs))
    from glados.core import config_store
    config_store.cfg.reload()
    assert config_store.cfg.services.llm_interactive.model == "qwen3-30b-a3b"
    assert config_store.cfg.services.llm_triage.model == "llama-3.2-1b-instruct"


def test_llm_triage_default():
    """Triage slot defaults to Llama-3.2-1B-Instruct so a fresh install
    routes triage subagents to the small fast model out of the box."""
    from glados.core.config_store import ServicesConfig
    s = ServicesConfig()
    assert s.llm_triage.model == "llama-3.2-1b-instruct"
```

- [ ] **Step 3: Run the new tests; verify they fail.**

```bash
python -m pytest tests/test_config_defaults.py::test_config_loads_legacy_ollama_keys tests/test_config_defaults.py::test_config_loads_new_llm_keys tests/test_config_defaults.py::test_llm_triage_default -v
```

Expected: 3 failures (`AttributeError` on `services.llm_interactive` / `services.llm_triage`).

- [ ] **Step 4: Update `ServicesConfig` to use new names + aliases.**

```python
# glados/core/config_store.py — replace the existing ollama_* fields

class ServicesConfig(BaseModel):
    """OpenAI-shaped LLM service slots. Field names use the `llm_*`
    prefix; `ollama_*` aliases are accepted on read for one release of
    backwards compatibility with operators' existing services.yaml.
    On save, only the new names are emitted.
    """

    model_config = ConfigDict(populate_by_name=True)

    tts: ServiceEndpoint = ServiceEndpoint(url="http://localhost:8015")
    stt: ServiceEndpoint = ServiceEndpoint(url="http://localhost:8015")
    api_wrapper: ServiceEndpoint = ServiceEndpoint(url="http://localhost:8015")
    vision: ServiceEndpoint = ServiceEndpoint(url="")

    llm_interactive: ServiceEndpoint = Field(
        default_factory=lambda: ServiceEndpoint(
            url="http://localhost:11434/v1/chat/completions",
            model="qwen3-30b-a3b",
        ),
        alias="ollama_interactive",
        validation_alias=AliasChoices("llm_interactive", "ollama_interactive"),
    )
    llm_autonomy: ServiceEndpoint = Field(
        default_factory=lambda: ServiceEndpoint(
            url="http://localhost:11434/v1/chat/completions",
            model="qwen3-30b-a3b",
        ),
        alias="ollama_autonomy",
        validation_alias=AliasChoices("llm_autonomy", "ollama_autonomy"),
    )
    llm_vision: ServiceEndpoint = Field(
        default_factory=lambda: ServiceEndpoint(
            url="http://localhost:11434/v1/chat/completions",
            model="qwen2.5-vl-3b-instruct",
        ),
        alias="ollama_vision",
        validation_alias=AliasChoices("llm_vision", "ollama_vision"),
    )
    llm_triage: ServiceEndpoint = Field(
        default_factory=lambda: ServiceEndpoint(
            url="http://localhost:11434/v1/chat/completions",
            model="llama-3.2-1b-instruct",
        ),
    )

    gladys_api: ServiceEndpoint = ServiceEndpoint(url="")
```

Add the import if missing:

```python
from pydantic import AliasChoices, ConfigDict, Field
```

- [ ] **Step 5: Save-side rewrite — confirm the dump uses new keys.**

The existing save path almost certainly uses `model_dump()` then writes via PyYAML. Pydantic with `populate_by_name=True` and `alias=...` will emit the alias on dump unless `by_alias=False` is set. We want NEW names emitted, so dump must use `by_alias=False` (the default). Audit `glados/core/config_store.py` for any `model_dump(by_alias=True)` and remove `by_alias=True` if present (only on `ServicesConfig` — leave other models alone).

```bash
grep -n "by_alias" glados/core/config_store.py
```

If a `by_alias=True` is found scoped to ServicesConfig, change it to `by_alias=False` (or drop the kwarg).

- [ ] **Step 6: Run the new tests; verify they pass.**

```bash
python -m pytest tests/test_config_defaults.py::test_config_loads_legacy_ollama_keys tests/test_config_defaults.py::test_config_loads_new_llm_keys tests/test_config_defaults.py::test_llm_triage_default -v
```

Expected: 3 PASS.

- [ ] **Step 7: Run the full suite to catch any code path that referenced `services.ollama_*` directly.**

```bash
python -m pytest -q
```

Expected: failures from `glados/core/engine.py`, `glados/webui/tts_ui.py`, and existing tests that referenced the old names. Note the failing files — they're handled in Task 1.

- [ ] **Step 8: Update the docstring at line 707 only — defer code-level call site updates to Task 1.**

```python
# Empty default → consumers resolve via cfg.service_model("llm_autonomy").
```

- [ ] **Step 9: Commit (note: full suite is intentionally still failing until Task 1 lands; commit message says so).**

```bash
git add glados/core/config_store.py tests/test_config_defaults.py
git commit -m "feat(config): rename ollama_* service slots to llm_*; add llm_triage

Schema-level rename. Pydantic AliasChoices keep operators' existing
services.yaml files (with ollama_* keys) parsing cleanly for one
release. On save, the dump emits the new llm_* names. New
llm_triage slot defaults to llama-3.2-1b-instruct.

This commit lands the schema; Task 1 of the autonomy-triage-split
plan migrates call sites in engine.py / webui/tts_ui.py — full
suite stays red between this commit and Task 1's commit."
```

---

## Task 1: Migrate engine + webui call sites to `services.llm_*`

**Goal:** All in-tree references to `svcs.ollama_*` rewritten to `svcs.llm_*`. Engine reconciler log messages reference the new field names. Full test suite returns to green.

**Files:**
- Modify: `glados/core/engine.py:164,193,194,195,196,201,211,221,230` (call sites + log messages)
- Modify: `glados/webui/tts_ui.py:76` (`_default_chat_model`)
- Modify: `glados/webui/tts_ui.py:4565-4610` (`_sync_glados_config_urls` + comments)
- Modify: `tests/test_glados_services_override.py` (test references)
- Modify: `tests/test_glados_config_url_sync.py` (test references)

**Acceptance Criteria:**
- [ ] `grep -rE "(svcs|services|cfg)\.ollama_(interactive|autonomy|vision)" glados/` returns ZERO matches.
- [ ] Engine reconciler logs say `services.llm_interactive.model` etc. (not `ollama_*`).
- [ ] Existing `test_glados_services_override.py` and `test_glados_config_url_sync.py` continue to verify the same behaviour, just under the new field names.
- [ ] Full suite passes.

**Verify:** `python -m pytest -q` → all green.

**Steps:**

- [ ] **Step 1: Sweep `glados/core/engine.py` for `svcs.ollama_*` references.**

```bash
grep -nE "svcs\.ollama_|services\.ollama_" glados/core/engine.py
```

Expected: lines 164, 193, 194, 195, 196 + log messages at 201, 211, 221, 230.

- [ ] **Step 2: Apply the rename via Edit (do NOT use sed — leave docstrings around it untouched).**

Replace each occurrence:
- `svcs.ollama_interactive` → `svcs.llm_interactive`
- `svcs.ollama_autonomy` → `svcs.llm_autonomy`
- In log f-strings: `services.ollama_interactive.model` → `services.llm_interactive.model` and similarly for autonomy.
- The docstring at line 164 mentioning the slot names also flips.

- [ ] **Step 3: Sweep `glados/webui/tts_ui.py`.**

```bash
grep -nE "ollama_(interactive|autonomy|vision)" glados/webui/tts_ui.py | head -20
```

Replace each call site and doc comment to use `llm_*`. The `_sync_glados_config_urls` block at lines 4565-4610 has a comment block describing the sync direction — update those too so the comments match the field names.

- [ ] **Step 4: Update the two test files referencing old names.**

```bash
grep -nE "ollama_(interactive|autonomy|vision)" tests/test_glados_services_override.py tests/test_glados_config_url_sync.py
```

Replace `ollama_*` → `llm_*` in test fixtures, dict literals, and assertions.

- [ ] **Step 5: Verify the sweep is clean.**

```bash
grep -rnE "(svcs|services|cfg)\.ollama_(interactive|autonomy|vision)" glados/ tests/
```

Expected: ZERO matches.

- [ ] **Step 6: Run the full suite.**

```bash
python -m pytest -q
```

Expected: ALL PASS (no skipped beyond the existing 5).

- [ ] **Step 7: Commit.**

```bash
git add glados/core/engine.py glados/webui/tts_ui.py tests/test_glados_services_override.py tests/test_glados_config_url_sync.py
git commit -m "refactor: migrate engine + webui to services.llm_* names

Mechanical sweep — every reference to svcs.ollama_interactive /
ollama_autonomy / ollama_vision rewritten to the llm_* names from
the schema rename in the prior commit. Engine reconciler log
messages updated for parity. Two test files updated to assert the
new field names; behaviour they verify is unchanged. Full suite
back to green."
```

---

## Task 2: Add `LLMConfig.for_slot()` resolver to autonomy llm_client

**Goal:** Callers stop hard-coding URL+model from `cfg.services.X.url` / `cfg.services.X.model`. Instead they ask for a slot by name and get a fully-built `LLMConfig`. Wrong-slot is a fast-fail with a clear error so future renames don't silently route to the wrong endpoint.

**Files:**
- Modify: `glados/autonomy/llm_client.py`
- Create: `tests/test_llm_config_for_slot.py`

**Acceptance Criteria:**
- [ ] `LLMConfig.for_slot("llm_triage")` returns an `LLMConfig` with the URL and model from `cfg.services.llm_triage`.
- [ ] Same for `"llm_interactive"`, `"llm_autonomy"`, `"llm_vision"`.
- [ ] Unknown slot → `ValueError` with the slot name in the message.
- [ ] Existing `LLMConfig` construction paths still work (no breaking change to `LLMConfig.__init__`).
- [ ] New test file passes; full suite green.

**Verify:** `python -m pytest tests/test_llm_config_for_slot.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write the failing test.**

```python
# tests/test_llm_config_for_slot.py
"""LLMConfig.for_slot resolves a slot name to URL+model from cfg.services."""

from __future__ import annotations

import pytest

from glados.autonomy.llm_client import LLMConfig


class TestLLMConfigForSlot:
    def test_resolves_llm_triage(self) -> None:
        cfg = LLMConfig.for_slot("llm_triage")
        assert cfg.model  # default is llama-3.2-1b-instruct
        assert cfg.url.startswith("http")

    def test_resolves_llm_interactive(self) -> None:
        cfg = LLMConfig.for_slot("llm_interactive")
        assert cfg.model
        assert cfg.url.startswith("http")

    def test_resolves_llm_autonomy(self) -> None:
        cfg = LLMConfig.for_slot("llm_autonomy")
        assert cfg.model

    def test_unknown_slot_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            LLMConfig.for_slot("not_a_slot")
        assert "not_a_slot" in str(exc.value)

    def test_passes_through_timeout_kwarg(self) -> None:
        cfg = LLMConfig.for_slot("llm_triage", timeout=5.0)
        assert cfg.timeout == 5.0
```

- [ ] **Step 2: Run; verify failure.**

```bash
python -m pytest tests/test_llm_config_for_slot.py -v
```

Expected: `AttributeError: type object 'LLMConfig' has no attribute 'for_slot'`.

- [ ] **Step 3: Implement the classmethod on `LLMConfig`.**

Append to `glados/autonomy/llm_client.py` inside the `LLMConfig` dataclass:

```python
    _ALLOWED_SLOTS: ClassVar[tuple[str, ...]] = (
        "llm_interactive",
        "llm_autonomy",
        "llm_triage",
        "llm_vision",
    )

    @classmethod
    def for_slot(cls, slot: str, *, timeout: float = 30.0) -> "LLMConfig":
        """Resolve an ``LLMConfig`` from one of the four well-known
        service slots in ``cfg.services``. Slot must be one of
        ``llm_interactive``, ``llm_autonomy``, ``llm_triage``,
        ``llm_vision``."""
        if slot not in cls._ALLOWED_SLOTS:
            raise ValueError(
                f"Unknown service slot {slot!r}; "
                f"expected one of {cls._ALLOWED_SLOTS}"
            )
        from glados.core.config_store import cfg
        endpoint = getattr(cfg.services, slot)
        return cls(url=endpoint.url, model=endpoint.model, timeout=timeout)
```

Required imports at the top of the file (add only what's missing):

```python
from typing import ClassVar
```

- [ ] **Step 4: Run; verify pass.**

```bash
python -m pytest tests/test_llm_config_for_slot.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Run full suite.**

```bash
python -m pytest -q
```

Expected: all green.

- [ ] **Step 6: Commit.**

```bash
git add glados/autonomy/llm_client.py tests/test_llm_config_for_slot.py
git commit -m "feat(llm_client): LLMConfig.for_slot() resolves service slot to config

Callers can ask for an LLMConfig by slot name (\"llm_triage\",
\"llm_interactive\", etc.) instead of hardcoding cfg.services.X.url
and cfg.services.X.model at every call site. Unknown slot raises
ValueError with the slot name — future renames or typos fail fast
instead of routing to a default model. 5 new tests cover the four
known slots plus the error path."
```

---

## Task 3: Enforce `MAX_AUTONOMY_USER_PROMPT_TOKENS` budget in `llm_call`

**Goal:** When an autonomy subagent passes a `user_prompt` larger than the configurable budget, truncate the OLDEST content with a sentinel marker and emit a WARNING log. Stops "Context size has been exceeded" errors from LM Studio when the conversation history bloats. Budget defaults to 8000 characters (≈2000 tokens) which fits comfortably in the smallest live ctx (the 4096 default LM Studio fall-back).

**Files:**
- Modify: `glados/autonomy/llm_client.py`
- Create: `tests/test_autonomy_llm_client_budget.py`

**Acceptance Criteria:**
- [ ] New module-level constant `MAX_AUTONOMY_USER_PROMPT_CHARS = 8000`.
- [ ] New helper `_truncate_user_prompt(prompt: str, budget: int) -> tuple[str, bool]` returns the (possibly-truncated) prompt and a `truncated` flag.
- [ ] `llm_call` calls the helper before building the request body. When `truncated=True`, emits `logger.warning("LLM call: user_prompt truncated from {} to {} chars", original_len, budget)`.
- [ ] Truncation strategy: keep the LAST `budget` characters (most recent context wins), prepended with a sentinel `"[…truncated…]\n\n"` so the model sees something explanatory.
- [ ] New test file covers: short-prompt no-op, long-prompt truncation with sentinel, exact-budget no-op, the WARNING log emission, and integrated `llm_call` mock check.
- [ ] Full suite green.

**Verify:** `python -m pytest tests/test_autonomy_llm_client_budget.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_autonomy_llm_client_budget.py
"""User-prompt size budget enforced inside llm_call. Stops LM Studio
'Context size exceeded' error chunks from the autonomy lane."""

from __future__ import annotations

from unittest.mock import patch

from glados.autonomy.llm_client import (
    LLMConfig,
    MAX_AUTONOMY_USER_PROMPT_CHARS,
    _truncate_user_prompt,
    llm_call,
)


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class TestTruncateUserPrompt:
    def test_short_prompt_unchanged(self) -> None:
        out, truncated = _truncate_user_prompt("hi", 100)
        assert out == "hi"
        assert truncated is False

    def test_exact_budget_unchanged(self) -> None:
        text = "x" * 100
        out, truncated = _truncate_user_prompt(text, 100)
        assert out == text
        assert truncated is False

    def test_long_prompt_truncates_oldest(self) -> None:
        text = "OLD" + ("x" * 200) + "RECENT"
        out, truncated = _truncate_user_prompt(text, 50)
        assert truncated is True
        assert "[…truncated…]" in out
        # Most-recent suffix is preserved
        assert out.endswith("RECENT")
        # Oldest prefix is gone
        assert "OLD" not in out

    def test_truncation_keeps_total_within_budget_plus_sentinel(self) -> None:
        text = "x" * 1000
        out, truncated = _truncate_user_prompt(text, 100)
        # Budget is 100 characters of original content;
        # sentinel adds a bounded prefix.
        assert truncated is True
        assert len(out) <= 100 + len("[…truncated…]\n\n")


class TestLLMCallBudget:
    def _config(self) -> LLMConfig:
        return LLMConfig(
            url="http://example/v1/chat/completions",
            model="m",
            timeout=5.0,
        )

    def test_oversized_user_prompt_logs_warning(self, caplog) -> None:
        from loguru import logger as _loguru_logger
        records: list[str] = []
        sink_id = _loguru_logger.add(lambda m: records.append(str(m)), level="DEBUG")
        try:
            payload = {"choices": [{"message": {"content": "ok"}}]}
            with patch("requests.post", return_value=_Resp(payload)):
                llm_call(
                    self._config(),
                    "sys",
                    "x" * (MAX_AUTONOMY_USER_PROMPT_CHARS + 5000),
                )
        finally:
            _loguru_logger.remove(sink_id)
        assert any("truncated" in r.lower() for r in records), records

    def test_truncated_prompt_actually_sent(self) -> None:
        """The POST body's user message must be the truncated form."""
        seen = {}

        def _capture(url, **kwargs):
            seen["body"] = kwargs.get("json")
            return _Resp({"choices": [{"message": {"content": "ok"}}]})

        with patch("requests.post", side_effect=_capture):
            llm_call(
                self._config(),
                "sys",
                "OLDEST" + ("x" * 20000) + "NEWEST",
            )
        user_msg = seen["body"]["messages"][-1]
        assert user_msg["role"] == "user"
        assert "OLDEST" not in user_msg["content"]
        assert user_msg["content"].endswith("NEWEST")
```

- [ ] **Step 2: Run; verify failures.**

```bash
python -m pytest tests/test_autonomy_llm_client_budget.py -v
```

Expected: ImportError on `MAX_AUTONOMY_USER_PROMPT_CHARS` and `_truncate_user_prompt`.

- [ ] **Step 3: Implement constant + helper + integrate into `llm_call`.**

Add at module top of `glados/autonomy/llm_client.py`:

```python
MAX_AUTONOMY_USER_PROMPT_CHARS = 8000
_TRUNCATION_SENTINEL = "[…truncated…]\n\n"


def _truncate_user_prompt(
    prompt: str, budget: int = MAX_AUTONOMY_USER_PROMPT_CHARS,
) -> tuple[str, bool]:
    """If ``prompt`` exceeds ``budget`` characters, drop the oldest
    content and prepend a sentinel so the model sees something
    explanatory. Returns ``(text, truncated)``."""
    if len(prompt) <= budget:
        return prompt, False
    return _TRUNCATION_SENTINEL + prompt[-budget:], True
```

Then in `llm_call`, before constructing `messages`:

```python
def llm_call(
    config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    json_response: bool = False,
) -> str | None:
    # Budget enforcement — autonomy callers occasionally pass a
    # decade of conversation history. Truncate to the most-recent
    # MAX_AUTONOMY_USER_PROMPT_CHARS chars so LM Studio's ctx
    # window is never the bottleneck.
    original_len = len(user_prompt)
    user_prompt, truncated = _truncate_user_prompt(user_prompt)
    if truncated:
        logger.warning(
            "LLM call: user_prompt truncated from %d to %d chars",
            original_len, len(user_prompt),
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    # … existing body unchanged …
```

- [ ] **Step 4: Run; verify pass.**

```bash
python -m pytest tests/test_autonomy_llm_client_budget.py -v
```

Expected: 6 PASS.

- [ ] **Step 5: Run full suite.**

```bash
python -m pytest -q
```

Expected: all green.

- [ ] **Step 6: Commit.**

```bash
git add glados/autonomy/llm_client.py tests/test_autonomy_llm_client_budget.py
git commit -m "feat(llm_client): cap user_prompt at 8000 chars with sentinel

Stops LM Studio 'Context size has been exceeded' error chunks from
the autonomy lane when subagents pass a megabyte of conversation
history. The truncator keeps the most-recent N chars (newer context
matters more for classification) and prepends [...truncated...] so
the model sees something explanatory. Caller doesn't need to do
anything — the budget is enforced inside llm_call. WARNING log
fires whenever truncation happens so we can spot subagents that
need a tighter prompt design.

6 new tests cover short / exact-budget / long / sentinel placement /
log emission / integrated POST verification."
```

---

## Task 4: Route `summarization` and `memory_writer` callers to `llm_triage`

**Goal:** Replace the hardcoded `cfg.service_model("ollama_autonomy")` resolution in summarization + memory classifier with `LLMConfig.for_slot("llm_triage")`. These are pure classification / summarization tasks with no persona — perfect fit for the small fast triage model. Behavior Observer split is deferred to Task 6.

**Files:**
- Modify: `glados/autonomy/summarization.py:81,149` (and surrounding `LLMConfig` construction)
- Modify: `glados/core/memory_writer.py:354,374` (and surrounding `LLMConfig` construction)
- Create: `tests/test_summarization_uses_triage_slot.py`
- Create: `tests/test_memory_writer_uses_triage_slot.py`

**Acceptance Criteria:**
- [ ] `summarization.compact_messages` builds its `LLMConfig` via `LLMConfig.for_slot("llm_triage")`.
- [ ] `summarization.extract_facts` does the same.
- [ ] `memory_writer`'s classifier and extractor llm_call sites do the same.
- [ ] New tests patch `requests.post` and assert the URL hit matches the triage slot's URL.
- [ ] Full suite passes.

**Verify:** `python -m pytest tests/test_summarization_uses_triage_slot.py tests/test_memory_writer_uses_triage_slot.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Read the existing call sites to lock in surrounding shape.**

```bash
grep -n -B 2 -A 5 "llm_call(" glados/autonomy/summarization.py
grep -n -B 2 -A 5 "llm_call(" glados/core/memory_writer.py
```

Note the variable name used for `LLMConfig` in each (likely `llm_config` or `config`), and the surrounding model-resolution calls.

- [ ] **Step 2: Write the failing test for summarization.**

```python
# tests/test_summarization_uses_triage_slot.py
"""compact_messages + extract_facts route through llm_triage slot."""

from __future__ import annotations

from unittest.mock import patch


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _ok(content="ok"):
    return _Resp({"choices": [{"message": {"content": content}}]})


def test_compact_messages_hits_triage_url(monkeypatch, tmp_path) -> None:
    """The POST URL must be the llm_triage slot's URL, not llm_autonomy."""
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    (cfgs / "services.yaml").write_text(
        "llm_autonomy:\n"
        "  url: http://wrong:11434/v1/chat/completions\n"
        "  model: should-not-be-used\n"
        "llm_triage:\n"
        "  url: http://triage:11434/v1/chat/completions\n"
        "  model: llama-3.2-1b-instruct\n"
    )
    monkeypatch.setenv("GLADOS_CONFIGS", str(cfgs))
    from glados.core import config_store
    config_store.cfg.reload()

    from glados.autonomy.summarization import compact_messages
    seen = {}

    def _capture(url, **kwargs):
        seen.setdefault("urls", []).append(url)
        return _ok("summary")

    with patch("requests.post", side_effect=_capture):
        compact_messages([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])

    assert any("triage" in u for u in seen["urls"]), seen
    assert not any("wrong" in u for u in seen["urls"]), seen
```

Repeat the same shape for `extract_facts` in the same file.

- [ ] **Step 3: Run; verify failure (POST hits llm_autonomy URL today).**

```bash
python -m pytest tests/test_summarization_uses_triage_slot.py -v
```

Expected: FAIL — assertion that URL contains "triage" fails.

- [ ] **Step 4: Update `summarization.compact_messages` and `extract_facts`.**

Replace the existing `LLMConfig(...)` construction with:

```python
from glados.autonomy.llm_client import LLMConfig, llm_call

# inside compact_messages():
llm_config = LLMConfig.for_slot("llm_triage")
```

Same edit in `extract_facts`.

- [ ] **Step 5: Run; verify pass.**

- [ ] **Step 6: Repeat steps 2-5 for `memory_writer.py:354,374`.**

Test file: `tests/test_memory_writer_uses_triage_slot.py`. Same fixture pattern. Edit memory_writer's two `LLMConfig` constructions to use `for_slot("llm_triage")`.

- [ ] **Step 7: Run full suite.**

```bash
python -m pytest -q
```

Expected: all green.

- [ ] **Step 8: Commit.**

```bash
git add glados/autonomy/summarization.py glados/core/memory_writer.py tests/test_summarization_uses_triage_slot.py tests/test_memory_writer_uses_triage_slot.py
git commit -m "feat(autonomy): route compaction + memory classifier to llm_triage

These four call sites are pure classification or summarization with
no persona involvement — they're a perfect fit for the small fast
llm_triage slot (default Llama-3.2-1B-Instruct). Their POST bodies
were unchanged; only the URL+model resolved differs. Net effect:
each call costs <200 ms instead of pinning a slot on the chat-quality
model for several seconds.

2 new test files patch requests.post and verify the URL hit is the
llm_triage slot's, not llm_autonomy."
```

---

## Task 5: Route Tier 2 disambiguator to `llm_triage`

**Goal:** The Tier 2 LLM disambiguator (`glados/intent/disambiguator.py`) is the per-utterance classification gate. Today it shares the heavy chat model with interactive replies. Move it to `llm_triage` so disambiguation costs ≈100 ms instead of multi-second LLM thinking.

**Files:**
- Modify: `glados/intent/disambiguator.py:1974-1977` (and surrounding `LLMConfig` construction inside the disambiguator class init)
- Modify: `tests/test_disambiguator.py` (existing, if present — fixture URL update only)

**Acceptance Criteria:**
- [ ] Disambiguator's `LLMConfig` resolves via `LLMConfig.for_slot("llm_triage")`.
- [ ] Existing disambiguator tests pass against the new resolution path (URL fixture updated if needed).
- [ ] Container log message at boot reads `Tier 2 disambiguator ready; ollama=<llm_triage URL>`.
- [ ] Full suite green.

**Verify:** `python -m pytest tests/ -k disambiguator -v` → all PASS.

**Steps:**

- [ ] **Step 1: Inspect disambiguator's current `LLMConfig` construction.**

```bash
grep -n -B 3 -A 8 "LLMConfig\|llm_call" glados/intent/disambiguator.py | head -40
```

Locate the constructor where `LLMConfig` is built (likely around the lines noted in the file map).

- [ ] **Step 2: Replace that construction with `LLMConfig.for_slot("llm_triage")`.**

Match the existing variable name in scope. If the constructor accepts `model` and `url` as args (so callers can override), keep that signature — just default to `LLMConfig.for_slot("llm_triage")` when neither is passed.

- [ ] **Step 3: Update the boot-log message in `__main__:_init_ha_client` (referenced from the engine startup log we saw at session start) so it logs which slot the disambiguator resolved to.**

Find with:

```bash
grep -n "Tier 2 disambiguator ready" glados/ -r
```

- [ ] **Step 4: Run disambiguator tests.**

```bash
python -m pytest tests/ -k disambiguator -v
```

Expected: PASS. If a fixture hardcodes a model name, update it to use the triage default (`llama-3.2-1b-instruct`).

- [ ] **Step 5: Run full suite.**

```bash
python -m pytest -q
```

- [ ] **Step 6: Commit.**

```bash
git add glados/intent/disambiguator.py tests/test_disambiguator.py
git commit -m "feat(intent): route Tier 2 disambiguator to llm_triage slot

Tier 2 fires on every chat utterance that doesn't resolve at Tier 1.
On the heavy chat model that's a multi-second classification cost
on every miss; on the small llm_triage model (Llama-3.2-1B by
default) it's <200 ms. Net effect: chitchat round-trips drop by
the disambiguation cost on Tier-1-misses, which is most chat turns.

Disambiguator constructor still accepts explicit model/url overrides
for test fixtures; the change is just the default resolution path."
```

---

## Task 6: WebUI Services tab — labels + `llm_triage` card

**Goal:** Operator-visible UI catches up with the schema. The Services card in System → Services renders a fourth card for `llm_triage` so the operator can change its model without editing raw YAML. Existing labels switch from "Ollama" terminology to "LLM".

**Files:**
- Modify: `glados/webui/static/ui.js` — the `renderLLMServicesPage` function (or whatever the Services tab renderer is named; locate via grep)

**Acceptance Criteria:**
- [ ] System → Services tab now shows four LLM cards: Interactive / Autonomy / Triage / Vision.
- [ ] Each card shows its current URL + model + a status dot (the existing pattern from prior cards).
- [ ] Saving a model change to the Triage card writes to `services.yaml` under the `llm_triage` key (verified by reloading the page).
- [ ] No broken card from the rename — labels read "LLM (Interactive)" / "LLM (Autonomy)" / "LLM (Triage)" / "LLM (Vision)".

**Verify:** Manual visual cycle through Services tab; `curl https://glados.example.com:8052/api/services` returns the new four-card shape.

**Steps:**

- [ ] **Step 1: Locate the Services renderer.**

```bash
grep -n "ollama_interactive\|Ollama Interactive\|renderLLMServicesPage\|renderServices" glados/webui/static/ui.js | head
```

- [ ] **Step 2: Update card labels — "Ollama Interactive" → "LLM (Interactive)" etc.**

Search the renderer function block, replace the hardcoded title strings.

- [ ] **Step 3: Add a fourth card for Triage.**

Mirror the existing Interactive card structure exactly. Same status dot, same model dropdown, same save button. Only the slot key (`llm_triage`) and the visible label differ.

- [ ] **Step 4: Verify the save endpoint already accepts `llm_triage`.**

The settings POST handler in `tts_ui.py` reads `services.yaml` via `cfg.services` (which now has `llm_triage` from Task 0). If the save handler uses a slot-name allow-list, add `llm_triage` to that list.

```bash
grep -n "llm_interactive\|llm_autonomy\|llm_vision\|services.*update\|api/services" glados/webui/tts_ui.py | head
```

If an explicit list is found, append `"llm_triage"` to it.

- [ ] **Step 5: Manual smoke (no automated test for this UI change — visual + curl).**

Bring up the WebUI in a browser → System → Services → confirm 4 cards render. Click Save on the Triage card with a no-op change and confirm the YAML round-trip preserves `llm_triage`.

```bash
ssh root@$GLADOS_SSH_HOST "grep llm_triage /srv/.../services.yaml"
```

Expected: the post-save file has `llm_triage:` block with the operator's chosen model.

- [ ] **Step 6: Commit.**

```bash
git add glados/webui/static/ui.js glados/webui/tts_ui.py
git commit -m "feat(webui): Services tab — LLM labels + Triage card

System → Services tab renders four LLM cards (Interactive /
Autonomy / Triage / Vision). Operator can change the triage model
without editing raw YAML. Labels swap from \"Ollama\" terminology
to \"LLM\" everywhere on this tab.

The save handler already round-trips the new schema after Task 0;
this commit just wires the UI."
```

---

## Task 7: Live verification + docs update

**Goal:** Deploy via `scripts/_local_deploy.py`, validate in the browser, update SESSION_STATE.md and CHANGES.md to reflect the shipped state.

**Files:**
- Modify: `C:\src\SESSION_STATE.md` (top-section refresh)
- Modify: `docs/CHANGES.md` (new Change entry)

**Acceptance Criteria:**
- [ ] `_local_deploy.py` completes with health=healthy.
- [ ] Operator-side: pull/load `llama-3.2-1b-instruct` on AIBox if not already loaded:
  ```powershell
  & "$env:USERPROFILE\.lmstudio\bin\lms.exe" load llama-3.2-1b-instruct --gpu max --context-length 8192 -y
  ```
- [ ] Container logs show NO `'Context size has been exceeded'` chunks for autonomy lane during a 5-minute observation window.
- [ ] WebUI chitchat round-trip <5 s (chitchat path on `qwen3-30b-a3b` with `/no_think` from the prior session's Path 2 still works).
- [ ] Tier 2 disambiguation visible in container logs as fast (<500 ms) calls hitting the triage URL.
- [ ] CHANGES.md gains a new entry summarizing the rename + triage split.
- [ ] SESSION_STATE.md top section updated with new HEAD and the autonomy issue marked resolved.

**Verify:** Run a chat through the WebUI; tail container logs for 5 min:

```bash
ssh root@$GLADOS_SSH_HOST "docker logs glados --since 5m 2>&1 | grep -E 'exceeded|truncated|triage|Tier 2'"
```

Expected: no `exceeded`; some `truncated` (proves the budget is firing on the largest subagents); `triage` URL hits visible; no errors.

**Steps:**

- [ ] **Step 1: Run the full suite locally one more time.**

```bash
python -m pytest -q
```

Expected: all green.

- [ ] **Step 2: Push to origin.**

```bash
git push origin webui-polish
```

- [ ] **Step 3: Deploy.**

```bash
env GLADOS_SSH_HOST=docker-host.local \
    GLADOS_SSH_USER=root \
    GLADOS_SSH_PASSWORD='<see SESSION_STATE.md>' \
    GLADOS_COMPOSE_PATH='/srv/.../docker-compose.yml' \
    MSYS_NO_PATHCONV=1 \
  python scripts/_local_deploy.py
```

Expected: tarball uploaded, image built remotely, container recreated, health=healthy.

- [ ] **Step 4: Operator-side: confirm Llama-3.2-1B loaded on AIBox.**

```powershell
& "$env:USERPROFILE\.lmstudio\bin\lms.exe" ps
```

If not loaded:

```powershell
& "$env:USERPROFILE\.lmstudio\bin\lms.exe" load llama-3.2-1b-instruct --gpu max --context-length 8192 -y
```

- [ ] **Step 5: Live verify: tail container logs for 5 min, run a few WebUI chats, confirm absence of error chunks.**

```bash
ssh root@$GLADOS_SSH_HOST "docker logs glados --since 5m -f 2>&1 | grep -vE 'home_assistant|deprecated|api/mcp|For more|HA Sensor' | head -100"
```

Send 2-3 chitchat messages and 1-2 home commands ("turn off the kitchen lights"). Watch for:
- chitchat replies streaming content quickly
- home command triggers thinking + emits a tool call
- autonomy retries no longer flooding (the `unexpected response format` and `Context size has been exceeded` lines should be absent)

- [ ] **Step 6: Update SESSION_STATE.md top section.**

Replace the "Remaining issue: autonomy-lane prompt size" block with a "Resolved" note and bump the deployed HEAD reference.

- [ ] **Step 7: Add CHANGES.md entry.**

Format matches the existing chronological style — "Change N+1 (2026-04-28): Autonomy triage split + service slot rename to llm_*". Cover schema rename, llm_triage slot, prompt-budget enforcement, summarization/memory_writer/disambiguator routing, and live-verified outcomes.

- [ ] **Step 8: Final commit.**

```bash
git add docs/CHANGES.md "C:\src\SESSION_STATE.md"
git commit -m "docs: 2026-04-28 autonomy triage split — live verification + CHANGES

Plan docs/superpowers/plans/2026-04-28-autonomy-triage-split.md
shipped end-to-end. Live container shows no Context-size-exceeded
errors from autonomy; Tier 2 disambiguator now answers in <500 ms;
chitchat round-trips comfortably under 5 s. SESSION_STATE.md top
section refreshed; CHANGES.md gains the corresponding entry."
git push origin webui-polish
```

---

## Self-Review

Spec coverage:
- ✅ `llm_triage` slot in services.yaml schema (Task 0)
- ✅ Field rename `ollama_*` → `llm_*` with one-release dual-key support (Task 0)
- ✅ Engine + WebUI call sites migrated (Task 1)
- ✅ `LLMConfig.for_slot()` resolver (Task 2)
- ✅ User-prompt budget enforcement (Task 3)
- ✅ Summarization + memory writer routed to triage (Task 4)
- ✅ Tier 2 disambiguator routed to triage (Task 5)
- ✅ WebUI Services card for the triage slot (Task 6)
- ✅ Live verify + docs (Task 7)
- ⏸ Behavior Observer split (deferred — flagged in plan body as risk #4)

Placeholder scan: no "TBD", "implement later", or unresolved references. Each step has the actual code or command an engineer needs.

Type consistency: `LLMConfig.for_slot(slot)` declared in Task 2, used as-is in Tasks 4-5. `_truncate_user_prompt(prompt, budget)` declared in Task 3, no later cross-references. `ServiceEndpoint` referenced consistently in Task 0 (it's the existing model from `config_store.py`).

Risks listed in the plan body:
1. Llama-3.2-1B classification quality — Stage B's prompt rewrites are independently valuable, so plan is safe-first-step.
2. Pydantic alias migration — operators with `ollama_*` keys keep working through one release; track cleanup release N+1.
3. WebUI label changes — operator-visible cosmetic; flagged in the Phase 2 follow-up notes.
4. Behavior Observer split — defer to Stage B.5 if it gets messy; not in this plan's critical path.
