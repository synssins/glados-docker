"""Structural tests for the Configuration form builder in tts_ui.py.

`cfgBuildForm` is a JS function embedded in the HTML_PAGE string. We do not
run a JS engine from pytest today, so these tests are structural: they assert
that the JS source in tts_ui.py has the right shape. Behavior is verified
interactively via the Claude Preview MCP during development; end-to-end DOM
assertions will be added once pytest-playwright is wired up.

Covers Stage 3 Phase 6 — Commit 1:
  - `cfgBuildForm` accepts a `skipKeys` parameter.
  - It skips those keys only at the top level (prefix empty) so a nested
    field happening to share a skipped name is not hidden.
  - The Global section passes `['ssl', 'paths', 'network']` so SSL (which
    has its own page) and env-driven path/network fields no longer render
    as duplicate groups on Global.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TTS_UI = Path(__file__).resolve().parent.parent / "glados" / "webui" / "tts_ui.py"


@pytest.fixture(scope="module")
def source() -> str:
    return TTS_UI.read_text(encoding="utf-8")


def test_cfg_build_form_accepts_skip_keys(source: str) -> None:
    assert re.search(
        r"function\s+cfgBuildForm\s*\(\s*obj\s*,\s*section\s*,\s*prefix\s*,\s*skipKeys\s*\)",
        source,
    ), "cfgBuildForm must accept a skipKeys parameter"


def test_cfg_build_form_skip_check_is_top_level_only(source: str) -> None:
    # The guard must use `!prefix` so nested keys aren't accidentally hidden.
    assert re.search(
        r"if\s*\(\s*skipKeys\s*&&\s*!prefix\s*&&\s*skipKeys\.indexOf\(key\)\s*!==\s*-1\s*\)",
        source,
    ), "skipKeys must only apply at the top level (prefix empty)"


def test_global_backing_skips_ssl_paths_network(source: str) -> None:
    # The 'global' backing dispatch in cfgRenderSection must forward the
    # skip list. This applies both when the virtual page is Integrations
    # (section=integrations, backing=global) and when a legacy direct call
    # uses section=global.
    pattern = re.compile(
        r"backing\s*===\s*'global'.*?\[\s*('ssl'|'paths'|'network')\s*,\s*"
        r"('ssl'|'paths'|'network')\s*,\s*('ssl'|'paths'|'network')\s*\]",
        re.DOTALL,
    )
    m = pattern.search(source)
    assert m, "Global backing must pass skipKeys=['ssl','paths','network'] to cfgBuildForm"
    assert set(m.groups()) == {"'ssl'", "'paths'", "'network'"}


def test_non_global_backings_do_not_inherit_skip_list(source: str) -> None:
    # Guard against accidentally skipping keys for other backings. The
    # ternary must gate on backing === 'global' (may or may not wrap in parens).
    assert re.search(
        r"backing\s*===\s*'global'\)?\s*\?\s*\[\s*'ssl'\s*,\s*'paths'\s*,\s*'network'\s*\]",
        source,
    ), (
        "skipKeys for cfgBuildForm must be gated on backing === 'global' "
        "so other pages (services/audio/etc.) are unaffected"
    )
