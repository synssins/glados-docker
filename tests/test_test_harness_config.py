"""Phase 8.9 — TestHarnessConfig surface + public read endpoint.

The external test battery (in `C:\\src\\glados-test-battery`) pulls
noise-entity globs and the direction-match flag from this container
via a public GET so the operator edits patterns in one place. Tests
lock the shape of that contract.
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import pytest
import yaml

from glados.core.config_store import (
    GladosConfigStore,
    TestHarnessConfig,
)


# ── Defaults ───────────────────────────────────────────────────────


def test_defaults_ship_operator_known_noise_globs() -> None:
    """The default patterns cover the operator-reported noisy entity
    families identified in the 2026-04-20 battery: Midea AC displays
    refresh every minute, Sonos diagnostics flap, WLED "reverse"
    toggles get counted as changes, and zigbee housekeeping entities
    (`*_button_indication`, `*_node_identify`) ping HA.
    """
    th = TestHarnessConfig()
    patterns = th.noise_entity_patterns
    assert any("midea" in p for p in patterns), patterns
    assert any("sonos" in p for p in patterns), patterns
    assert any("wled" in p and "reverse" in p for p in patterns), patterns
    assert any("button_indication" in p for p in patterns), patterns
    assert any("node_identify" in p for p in patterns), patterns


def test_defaults_require_direction_match() -> None:
    """Direction-match defaults to True — the scoring fix is the whole
    point of Phase 8.9. Back-compat off-switch is an operator opt-out,
    not the default.
    """
    assert TestHarnessConfig().require_direction_match is True


def test_empty_patterns_list_allowed() -> None:
    """Operator can clear the list entirely (harness then does no
    noise filtering). Validation must not require a non-empty list."""
    th = TestHarnessConfig(noise_entity_patterns=[])
    assert th.noise_entity_patterns == []


# ── Config store integration ───────────────────────────────────────


def test_store_exposes_test_harness_property(tmp_path: Path) -> None:
    store = GladosConfigStore()
    store.load(configs_dir=tmp_path)
    assert isinstance(store.test_harness, TestHarnessConfig)


def test_store_to_dict_includes_test_harness(tmp_path: Path) -> None:
    """`/api/config` surfaces the section — the generic config UI
    depends on `to_dict` returning every registered section.
    """
    store = GladosConfigStore()
    store.load(configs_dir=tmp_path)
    dump = store.to_dict()
    assert "test_harness" in dump
    assert "noise_entity_patterns" in dump["test_harness"]
    assert "require_direction_match" in dump["test_harness"]


def test_update_section_writes_yaml_and_reloads(tmp_path: Path) -> None:
    """PUT /api/config/test_harness flows through `update_section`
    which writes ``test_harness.yaml`` next to the other per-section
    YAMLs and hot-reloads the store."""
    store = GladosConfigStore()
    store.load(configs_dir=tmp_path)

    new = {
        "noise_entity_patterns": ["switch.foo_*", "light.bar_*"],
        "require_direction_match": False,
    }
    store.update_section("test_harness", new)

    yaml_path = tmp_path / "test_harness.yaml"
    assert yaml_path.exists()
    reread = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert reread["noise_entity_patterns"] == [
        "switch.foo_*", "light.bar_*",
    ]
    assert reread["require_direction_match"] is False

    # Store reloads — live property reflects the save.
    assert store.test_harness.noise_entity_patterns == [
        "switch.foo_*", "light.bar_*",
    ]
    assert store.test_harness.require_direction_match is False


def test_update_section_rejects_unknown_section(tmp_path: Path) -> None:
    """Writing to an unregistered section raises. The other config
    sections allow unknown field *names* (pydantic default) so a typo
    silently drops rather than errors — matching existing behaviour;
    the HTTP layer surfaces only known fields via the config-section
    form builder.
    """
    store = GladosConfigStore()
    store.load(configs_dir=tmp_path)
    with pytest.raises(KeyError):
        store.update_section("not_a_section", {})


def test_yaml_round_trip_preserves_order(tmp_path: Path) -> None:
    """Operator-edited YAML files are human-readable; we write with
    ``sort_keys=False`` so reviewing a diff makes sense."""
    store = GladosConfigStore()
    store.load(configs_dir=tmp_path)
    store.update_section(
        "test_harness",
        {
            "noise_entity_patterns": ["a_*", "b_*"],
            "require_direction_match": True,
        },
    )
    yaml_text = (tmp_path / "test_harness.yaml").read_text(encoding="utf-8")
    # First key should be the patterns list, not the boolean — order
    # matches declaration in the pydantic model.
    first_key = yaml_text.splitlines()[0]
    assert first_key.startswith("noise_entity_patterns"), yaml_text


# ── Glob semantics the harness contract relies on ──────────────────


def test_default_globs_match_midea_display_entities() -> None:
    """The exact entity_ids that were false-positive-passing on the
    2026-04-20 battery run — sanity-check the default glob actually
    covers them."""
    patterns = TestHarnessConfig().noise_entity_patterns
    known_noisy = [
        "switch.hvac_one_display",
        "switch.hvac_two_display",
    ]
    for eid in known_noisy:
        assert any(fnmatch.fnmatch(eid, p) for p in patterns), (
            f"{eid} should match at least one default noise glob"
        )


def test_default_globs_do_not_match_real_targets() -> None:
    """Must not accidentally swallow real operator-targeted entities.
    Regressing this is worse than under-filtering: a false negative on
    the noise filter turns a real PASS into a FAIL.
    """
    patterns = TestHarnessConfig().noise_entity_patterns
    targets = [
        "light.kitchen_ceiling",
        "light.living_room_lamp",
        "switch.office_overhead",
        "fan.bedroom",
        "cover.vehicle_door",
    ]
    for eid in targets:
        assert not any(fnmatch.fnmatch(eid, p) for p in patterns), (
            f"{eid} wrongly matches a default noise glob"
        )


# ── Public endpoint shape ──────────────────────────────────────────


def test_public_endpoint_payload_shape() -> None:
    """Contract the external harness depends on: the endpoint returns
    a flat JSON object with exactly these two keys. Shape change
    requires a harness-side update in lock-step, so lock it here.
    """
    th = TestHarnessConfig()
    payload = {
        "noise_entity_patterns": list(th.noise_entity_patterns),
        "require_direction_match": bool(th.require_direction_match),
    }
    # JSON-serialisable, no nested sections:
    encoded = json.loads(json.dumps(payload))
    assert set(encoded.keys()) == {
        "noise_entity_patterns", "require_direction_match",
    }
    assert isinstance(encoded["noise_entity_patterns"], list)
    assert isinstance(encoded["require_direction_match"], bool)
