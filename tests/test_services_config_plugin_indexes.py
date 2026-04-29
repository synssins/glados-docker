"""ServicesConfig.plugin_indexes round-trip + validation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from glados.core.config_store import ServicesConfig


def test_plugin_indexes_default_is_empty():
    cfg = ServicesConfig()
    assert cfg.plugin_indexes == []


def test_plugin_indexes_https_only():
    with pytest.raises(ValidationError, match="https"):
        ServicesConfig(plugin_indexes=["http://x.test/index.json"])


def test_plugin_indexes_accepts_https_list():
    cfg = ServicesConfig(plugin_indexes=[
        "https://raw.githubusercontent.com/synssins/glados-plugins/main/index.json",
        "https://example.test/community/index.json",
    ])
    assert len(cfg.plugin_indexes) == 2


def test_plugin_indexes_round_trip(tmp_path):
    """Save -> load via YAML."""
    import yaml
    cfg = ServicesConfig(plugin_indexes=["https://x.test/i.json"])
    dump = cfg.model_dump(mode="json")
    yaml_text = yaml.safe_dump(dump)
    parsed = yaml.safe_load(yaml_text)
    cfg2 = ServicesConfig.model_validate(parsed)
    assert cfg2.plugin_indexes == ["https://x.test/i.json"]
