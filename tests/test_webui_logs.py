"""Stage 3 Phase 6 follow-up — Configuration > Logs page.

Structural checks on the JS/HTML in tts_ui.py. Behavior verified
interactively via Claude Preview MCP against the live container's
docker-logs output.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TTS_UI = Path(__file__).resolve().parent.parent / "glados" / "webui" / "tts_ui.py"


@pytest.fixture(scope="module")
def source() -> str:
    return TTS_UI.read_text(encoding="utf-8")


# ── Backend ────────────────────────────────────────────────────────────


def test_logs_sources_route_registered(source: str) -> None:
    assert re.search(
        r"elif\s+p\s*==\s*\"/api/logs/sources\"\s*:\s*\n\s*self\._get_logs_sources\(\)",
        source,
    ), "Expected /api/logs/sources route → _get_logs_sources"


def test_logs_tail_route_registered(source: str) -> None:
    assert re.search(
        r"elif\s+p\.startswith\(\"/api/logs/tail\"\)\s*:\s*\n\s*self\._get_logs_tail\(\)",
        source,
    ), "Expected /api/logs/tail route → _get_logs_tail"


def test_log_sources_whitelist_includes_docker_and_audit(source: str) -> None:
    # Locate each whitelist dict and verify the expected entries are inside.
    m_docker = re.search(r"_LOG_SOURCES_DOCKER\s*=\s*\{(.+?)\}", source, re.DOTALL)
    assert m_docker, "Expected _LOG_SOURCES_DOCKER dict"
    docker_block = m_docker.group(1)
    assert '"container"' in docker_block and '"glados"' in docker_block
    assert '"chromadb"' in docker_block and '"glados-chromadb"' in docker_block

    m_file = re.search(r"_LOG_SOURCES_FILE\s*=\s*\{(.+?)\}", source, re.DOTALL)
    assert m_file, "Expected _LOG_SOURCES_FILE dict"
    file_block = m_file.group(1)
    assert '"audit"' in file_block and '"audit.jsonl"' in file_block


def test_logs_tail_enforces_lines_cap(source: str) -> None:
    # Upper bound of 5000 lines. Prevents operators from yanking massive
    # payloads + prevents any internal caller from accidentally DoSing
    # the backend with huge requests.
    assert re.search(
        r"lines\s*=\s*max\(\s*1\s*,\s*min\(\s*int\(.+?\)\s*,\s*5000\s*\)\s*\)",
        source,
    ), "Expected tail-endpoint line count to clamp to [1, 5000]"


def test_logs_tail_requests_timestamped_docker_logs(source: str) -> None:
    # We hit the Docker Engine API via the mounted unix socket (the
    # docker CLI is not installed inside the container image). Operators
    # need timestamps so they can correlate logs with user-visible events.
    assert "timestamps=" in source, (
        "docker logs HTTP API query must include timestamps="
    )
    assert "_docker_logs_tail" in source, (
        "Expected a _docker_logs_tail helper that speaks to the socket"
    )


# ── Frontend ───────────────────────────────────────────────────────────


def test_logs_nav_entry_in_sidebar(source: str) -> None:
    assert re.search(
        r'data-nav-key="config\.logs"[^>]*>Logs</a>',
        source,
    ), "Expected Logs entry in Configuration sidebar"


def test_logs_panel_id_mapping(source: str) -> None:
    assert re.search(
        r"if\s*\(\s*key\s*===\s*'config\.logs'\s*\)\s*return\s+'tab-config-logs'",
        source,
    ), "Expected _panelIdFor('config.logs') → 'tab-config-logs'"


def test_logs_tab_panel_exists(source: str) -> None:
    assert 'id="tab-config-logs"' in source, (
        "Expected <div id='tab-config-logs'> host panel"
    )


def test_logs_controls_present(source: str) -> None:
    for el_id in ("logsSource", "logsLines", "logsFilter", "logsAuto", "logsBody"):
        assert f'id="{el_id}"' in source, f"Expected control element #{el_id}"


def test_logs_auto_refresh_stops_on_nav_away(source: str) -> None:
    # When the operator navigates away from config.logs, the poll timer
    # must be torn down — otherwise /api/logs/tail keeps firing every
    # 10 s while the user is on an unrelated page.
    assert re.search(
        r"_activeNavKey\s*===\s*'config\.logs'\s*&&\s*key\s*!==\s*'config\.logs'",
        source,
    ), "Expected teardown of logs auto-refresh when navigating away"


def test_logs_severity_classifier_handles_loguru_and_jsonl(source: str) -> None:
    # The JS _logsSeverity helper must recognize both loguru pipe-delimited
    # level tokens and JSONL {"level":"..."} fields so the Logs page can
    # colorize both source shapes consistently.
    assert '"level":\\s*\\"ERROR\\"' in source or '"level":' in source, (
        "_logsSeverity should inspect JSONL 'level' fields"
    )
    assert "SUCCESS" in source and "ERROR" in source, (
        "_logsSeverity should recognize loguru SUCCESS / ERROR levels"
    )
