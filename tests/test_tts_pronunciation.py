"""Phase 8.10 — operator-editable pre-TTS pronunciation overrides.

Tests the two injection surfaces:

1. ``TtsPronunciationConfig`` — pydantic section, defaults, YAML
   round-trip, config-store registration.
2. ``SpokenTextConverter`` — ``symbol_expansions`` + ``word_expansions``
   run BEFORE the all-caps splitter so ``"AI"`` emits ``"Aye Eye"``
   instead of the pre-8.10 ``"A I"`` slur.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from glados.core.config_store import (
    GladosConfigStore,
    TtsPronunciationConfig,
)
from glados.utils.spoken_text_converter import SpokenTextConverter


# ── TtsPronunciationConfig defaults ────────────────────────────────


def test_defaults_cover_operator_flagged_cases() -> None:
    cfg = TtsPronunciationConfig()
    assert cfg.word_expansions.get("AI") == "Aye Eye"
    assert cfg.word_expansions.get("HA") == "Home Assistant"
    assert cfg.symbol_expansions.get("%") == " percent"


def test_defaults_include_common_acronym_starter_set() -> None:
    """Regression: starter set added 2026-04-28 — without these,
    Piper renders ``NASA`` / ``CPU`` / ``FBI`` etc. as slurred
    letter-by-letter sounds via the all-caps splitter."""
    cfg = TtsPronunciationConfig()
    # Pronounced as words
    for k in ("NASA", "NATO", "HVAC", "JSON"):
        assert k in cfg.word_expansions, f"missing word-form acronym {k}"
    # Civic / org initialisms
    for k in ("FBI", "CIA", "NSA", "IRS"):
        assert k in cfg.word_expansions, f"missing civic acronym {k}"
    # Computing fundamentals
    for k in ("CPU", "GPU", "RAM", "USB", "URL", "API"):
        assert k in cfg.word_expansions, f"missing computing acronym {k}"
    # Container domain
    for k in ("LLM", "TTS", "STT", "MQTT", "MCP"):
        assert k in cfg.word_expansions, f"missing container-domain acronym {k}"


def test_starter_set_renders_through_converter() -> None:
    """End-to-end: defaults flow into the converter and produce
    expanded output for representative cases."""
    cfg = TtsPronunciationConfig()
    stc = SpokenTextConverter(
        symbol_expansions=dict(cfg.symbol_expansions),
        word_expansions=dict(cfg.word_expansions),
    )
    cases = [
        ("NASA launched a probe.", "nassa"),
        ("My CPU is hot.", "see pee you"),
        ("Check the URL.", "you are ell"),
        ("FBI agent.", "eff bee eye"),
        ("HVAC is broken.", "h vack"),
    ]
    for src, expected in cases:
        out = stc.text_to_spoken(src).lower()
        assert expected in out, f"{src!r} → {out!r} missing {expected!r}"


def test_defaults_allow_empty_maps() -> None:
    cfg = TtsPronunciationConfig(
        symbol_expansions={}, word_expansions={},
    )
    assert cfg.symbol_expansions == {}
    assert cfg.word_expansions == {}


# ── Config store integration ───────────────────────────────────────


def test_store_exposes_tts_pronunciation_property(tmp_path: Path) -> None:
    store = GladosConfigStore()
    store.load(configs_dir=tmp_path)
    assert isinstance(store.tts_pronunciation, TtsPronunciationConfig)


def test_to_dict_includes_section(tmp_path: Path) -> None:
    store = GladosConfigStore()
    store.load(configs_dir=tmp_path)
    dump = store.to_dict()
    assert "tts_pronunciation" in dump
    assert "symbol_expansions" in dump["tts_pronunciation"]
    assert "word_expansions" in dump["tts_pronunciation"]


def test_update_section_yaml_round_trip(tmp_path: Path) -> None:
    store = GladosConfigStore()
    store.load(configs_dir=tmp_path)
    new = {
        "symbol_expansions": {"%": " percent", "+": " plus "},
        "word_expansions": {"SSL": "S S L", "AI": "Aye Eye"},
    }
    store.update_section("tts_pronunciation", new)
    yaml_path = tmp_path / "tts_pronunciation.yaml"
    assert yaml_path.exists()
    reread = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert reread["word_expansions"]["SSL"] == "S S L"
    assert reread["symbol_expansions"]["+"] == " plus "
    assert store.tts_pronunciation.word_expansions["SSL"] == "S S L"


# ── SpokenTextConverter pre-pass ───────────────────────────────────


def test_ai_expands_before_allcaps_splitter() -> None:
    """Pre-8.10 ``"AI"`` became ``"A I"`` via the all-caps splitter
    and Piper slurred it to "Aye". With the pre-pass, the configured
    expansion ``"Aye Eye"`` lands intact (case-folded to lowercase by
    the downstream word-casing pass, which is fine — TTS reads
    lowercase and mixed-case identically)."""
    stc = SpokenTextConverter(word_expansions={"AI": "Aye Eye"})
    out = stc.text_to_spoken("AI is fascinating").lower()
    assert "aye eye" in out
    assert "a i " not in out
    assert " a i" not in out


def test_ha_expansion_case_insensitive() -> None:
    stc = SpokenTextConverter(word_expansions={"HA": "Home Assistant"})
    # All three casings of HA match
    for src in ("HA is slow today", "ha is slow today", "Ha is slow today"):
        assert "home assistant" in stc.text_to_spoken(src).lower()


def test_word_boundary_prevents_substring_match() -> None:
    """``"AI"`` mustn't match inside words like ``"AIRPORT"`` or
    ``"Bali"`` — regression check for partial-match bugs.
    """
    stc = SpokenTextConverter(word_expansions={"AI": "Aye Eye"})
    out = stc.text_to_spoken("The airport in Bali").lower()
    assert "aye eye" not in out  # "AIRPORT" / "Bali" not expanded
    # Existing all-caps splitter still runs; "airport" stays lowercase
    assert "airport" in out


def test_symbol_expansions_literal() -> None:
    stc = SpokenTextConverter(
        symbol_expansions={"%": " percent", "&": " and "},
    )
    assert "percent" in stc.text_to_spoken("at 80%").lower()
    assert "and" in stc.text_to_spoken("R&B music").lower()


def test_longer_keys_match_before_shorter() -> None:
    """``"IoT"`` must match before ``"I"`` — the constructor sorts
    keys by length descending so the longer pattern wins."""
    stc = SpokenTextConverter(word_expansions={
        "IoT": "I o T",
        "I": "One",
    })
    out = stc.text_to_spoken("The IoT device").lower()
    assert "i o t" in out


def test_no_overrides_preserves_pre_8_10_behaviour() -> None:
    """Back-compat: ``SpokenTextConverter()`` with no args behaves
    exactly like before Phase 8.10."""
    stc = SpokenTextConverter()
    out = stc.text_to_spoken("AI runs hot")
    # Without overrides, the existing all-caps splitter activates
    assert "A I" in out


def test_empty_maps_preserve_pre_8_10_behaviour() -> None:
    """Explicitly-empty dicts are equivalent to no args."""
    stc = SpokenTextConverter(symbol_expansions={}, word_expansions={})
    out = stc.text_to_spoken("AI runs hot")
    assert "A I" in out


def test_expansion_applied_before_number_processing() -> None:
    """``"80%"`` → ``"80 percent"`` → number formatter sees ``"80"``
    cleanly. If the % weren't expanded first, the number pass could
    choke on ``"80%"``."""
    stc = SpokenTextConverter(symbol_expansions={"%": " percent"})
    out = stc.text_to_spoken("at 80%").lower()
    assert "percent" in out
    # Number formatting unaffected
    assert "80" in out or "eighty" in out


def test_whole_word_match_handles_adjacency() -> None:
    """``"AI."`` (with trailing punctuation) still matches — regex
    word boundary works at punctuation."""
    stc = SpokenTextConverter(word_expansions={"AI": "Aye Eye"})
    assert "aye eye" in stc.text_to_spoken("The AI.").lower()


def test_mixed_case_key_preserved_if_operator_spelled_it() -> None:
    """Operator sets ``"IoT"`` → ``"I o T"``. Any casing in input
    matches; output is the operator's spelling modulo downstream
    word-casing (which folds to lowercase)."""
    stc = SpokenTextConverter(word_expansions={"IoT": "I o T"})
    for src in ("IoT", "iot", "IOT"):
        assert "i o t" in stc.text_to_spoken(src).lower()


# ── Pronunciation section shape (for public API contract) ──────────


def test_section_dump_is_pure_json_compatible() -> None:
    """``.model_dump()`` yields JSON-serialisable dicts — the WebUI
    save path round-trips via JSON, so nested non-serialisable types
    would break that path."""
    import json
    cfg = TtsPronunciationConfig()
    json.dumps(cfg.model_dump())  # must not raise
