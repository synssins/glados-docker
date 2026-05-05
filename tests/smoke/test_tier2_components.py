"""Tier 2 — Component reachability.

Each voice / integration component is exercised independently with the
smallest possible request that proves "wired up". Read-only against
GLaDOS state. Auth-gated tests skip cleanly when login fails.

Per TEST_PLAN.md §"Tier 2".
"""

from __future__ import annotations

import re

import pytest
import requests

pytestmark = pytest.mark.tier2


# ─── Voice pipeline (in-container) ───────────────────────────────────────


def test_tier2_tts_voices_listed(http_session, smoke_config, smoke_record) -> None:
    """`/v1/voices` lists the bundled `glados` voice."""
    url = smoke_config.url("api", "/v1/voices")
    smoke_record.checked = f"GET {url}"
    smoke_record.expected = 'voices list contains "glados"'

    r = http_session.get(url, timeout=smoke_config.timeouts["default"])
    body = _safe_json(r)
    smoke_record.actual = body

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    voices = body.get("voices") or body.get("data") or []
    if isinstance(voices, list) and voices and isinstance(voices[0], dict):
        voice_ids = [v.get("id") or v.get("name") for v in voices]
    else:
        voice_ids = list(voices)
    smoke_record.extras["voice_ids"] = voice_ids
    assert "glados" in voice_ids, f"glados voice not in list: {voice_ids}"
    smoke_record.summary = f"{len(voice_ids)} voice(s) listed; glados present"


def test_tier2_tts_synth_smoke(http_session, smoke_config, smoke_record) -> None:
    """`/v1/audio/speech` returns audio bytes for a tiny input."""
    url = smoke_config.url("api", "/v1/audio/speech")
    smoke_record.checked = f"POST {url} with input='smoke test', voice='glados'"
    smoke_record.expected = "200 + Content-Type starts with 'audio/' + body > 1024 B"

    r = http_session.post(
        url,
        json={
            "input": "smoke test",
            "voice": "glados",
            "response_format": "wav",
        },
        timeout=smoke_config.timeouts["tts"],
    )
    ctype = r.headers.get("Content-Type", "")
    body_len = len(r.content)
    smoke_record.actual = f"{r.status_code} ct={ctype!r} bytes={body_len}"
    smoke_record.extras["content_type"] = ctype
    smoke_record.extras["bytes"] = body_len

    assert r.status_code == 200, f"unexpected status {r.status_code}: {r.text[:300]}"
    assert ctype.startswith("audio/"), f"content-type not audio/*: {ctype!r}"
    assert body_len > 1024, f"audio body too small: {body_len} bytes"
    smoke_record.summary = f"TTS returned {body_len} bytes of {ctype}"


def test_tier2_stt_route_alive(http_session, smoke_config, smoke_record) -> None:
    """`/v1/audio/transcriptions` route is registered (rejects empty body
    with 4xx, NOT 404)."""
    url = smoke_config.url("api", "/v1/audio/transcriptions")
    smoke_record.checked = f"POST {url} with no file field"
    smoke_record.expected = "4xx (route exists, rejects malformed input)"

    r = http_session.post(
        url,
        files={},
        timeout=smoke_config.timeouts["default"],
    )
    smoke_record.actual = f"{r.status_code} {r.text[:200]}"

    assert 400 <= r.status_code < 500, (
        f"expected 4xx (route exists), got {r.status_code}"
    )
    assert r.status_code != 404, "route is missing — handler not registered"
    smoke_record.summary = (
        f"STT route registered ({r.status_code}); fixture-based decode in Tier 3"
    )


def test_tier2_llm_models_listed(http_session, smoke_config, smoke_record) -> None:
    """`/v1/models` returns the canonical `glados` model entry."""
    url = smoke_config.url("api", "/v1/models")
    smoke_record.checked = f"GET {url}"
    smoke_record.expected = "200 + data[].id contains 'glados'"

    r = http_session.get(url, timeout=smoke_config.timeouts["default"])
    body = _safe_json(r)
    smoke_record.actual = body

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    data = body.get("data") or body.get("models") or []
    if isinstance(data, list) and data and isinstance(data[0], dict):
        ids = [m.get("id") for m in data]
    else:
        ids = list(data)
    smoke_record.extras["model_ids"] = ids
    assert "glados" in ids, f"glados model id not in {ids}"
    smoke_record.summary = f"{len(ids)} model(s) listed; glados present"


@pytest.mark.requires_auth
def test_tier2_llm_slots_configured(
    auth_http_session, smoke_config, smoke_record
) -> None:
    """All four LLM slots have non-empty URLs.

    Slots live under the `services` config section as
    `llm_interactive` / `llm_autonomy` / `llm_triage` / `llm_vision`
    (per `glados/core/config_store.py:ServicesConfig`).
    """
    url = smoke_config.url("webui", "/api/config/services")
    smoke_record.checked = f"GET {url} (auth) — llm_* slots"
    smoke_record.expected = (
        "llm_interactive/autonomy/triage/vision each have non-empty url"
    )

    r = auth_http_session.get(url, timeout=smoke_config.timeouts["default"])
    body = _safe_json(r)

    assert r.status_code == 200, (
        f"unexpected status {r.status_code}: {body}"
    )
    assert isinstance(body, dict), (
        f"expected dict, got {type(body).__name__}"
    )

    expected_slots = ["llm_interactive", "llm_autonomy", "llm_triage", "llm_vision"]
    slot_summary: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    for slot in expected_slots:
        entry = body.get(slot)
        slot_url = (entry or {}).get("url", "") if isinstance(entry, dict) else ""
        slot_model = (entry or {}).get("model", "") if isinstance(entry, dict) else ""
        slot_summary[slot] = {"url": slot_url, "model": slot_model}
        if not slot_url.strip():
            missing.append(slot)

    smoke_record.extras["slots"] = slot_summary
    smoke_record.actual = {k: bool(v["url"]) for k, v in slot_summary.items()}

    if missing:
        smoke_record.summary = f"slot(s) without url: {', '.join(missing)}"
        pytest.fail(f"slot(s) have empty url: {missing}")
    smoke_record.summary = "All four LLM slots have URLs configured"


# ─── Integrations ────────────────────────────────────────────────────────


def test_tier2_ha_entities_present(
    http_session, smoke_config, smoke_record
) -> None:
    """`/entities` returns at least one HA entity (cache populated)."""
    url = smoke_config.url("api", "/entities")
    smoke_record.checked = f"GET {url}"
    smoke_record.expected = "200 + non-empty entity collection"

    r = http_session.get(url, timeout=smoke_config.timeouts["default"])
    body = _safe_json(r)
    smoke_record.actual = (
        f"{r.status_code} ({_len_or_keys(body)})"
    )

    assert r.status_code == 200, f"unexpected status {r.status_code}"

    if isinstance(body, list):
        count = len(body)
    elif isinstance(body, dict):
        # Body may wrap entities under a key.
        for key in ("entities", "data", "items"):
            if isinstance(body.get(key), (list, dict)):
                count = len(body[key])
                break
        else:
            count = len(body)
    else:
        count = 0

    smoke_record.extras["entity_count"] = count
    assert count > 0, "entity cache is empty — HA WS may not be authenticated"
    smoke_record.summary = f"{count} HA entities reachable via cache"


@pytest.mark.requires_auth
def test_tier2_ha_aggregate_ok(
    auth_http_session, smoke_config, smoke_record
) -> None:
    """`/api/health/aggregate` reports HA service ok."""
    url = smoke_config.url("webui", "/api/health/aggregate")
    smoke_record.checked = f"GET {url} (auth)"
    smoke_record.expected = "services.HA == ok"

    r = auth_http_session.get(url, timeout=smoke_config.timeouts["ha_aggregate"])
    body = _safe_json(r)
    smoke_record.actual = body

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    services = _services_dict(body)
    smoke_record.extras["services"] = services
    ha_status = services.get("HA")
    assert ha_status == "ok", f"HA status is {ha_status!r}"
    smoke_record.summary = "HA reachable via aggregate health probe"


@pytest.mark.requires_auth
def test_tier2_chromadb_writable(
    auth_http_session, smoke_config, smoke_record
) -> None:
    """`/api/health/aggregate` reports ChromaDB path writable."""
    url = smoke_config.url("webui", "/api/health/aggregate")
    smoke_record.checked = f"GET {url} (auth) — ChromaDB row"
    smoke_record.expected = "services.ChromaDB == ok"

    r = auth_http_session.get(url, timeout=smoke_config.timeouts["ha_aggregate"])
    body = _safe_json(r)
    smoke_record.actual = body

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    services = _services_dict(body)
    smoke_record.extras["services"] = services
    chroma_status = services.get("ChromaDB")
    assert chroma_status == "ok", f"ChromaDB status is {chroma_status!r}"
    smoke_record.summary = "ChromaDB store writable"


@pytest.mark.requires_auth
def test_tier2_mcp_plugins_listed(
    auth_http_session, smoke_config, smoke_record
) -> None:
    """`/api/plugins` returns a list-shaped body (count >= 0)."""
    url = smoke_config.url("webui", "/api/plugins")
    smoke_record.checked = f"GET {url} (auth)"
    smoke_record.expected = "200 + list-shaped body"

    r = auth_http_session.get(url, timeout=smoke_config.timeouts["default"])
    body = _safe_json(r)
    smoke_record.actual = f"{r.status_code} ({_len_or_keys(body)})"

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    if isinstance(body, list):
        count = len(body)
    elif isinstance(body, dict):
        count = len(body.get("plugins") or body.get("data") or body)
    else:
        count = 0
    smoke_record.extras["plugin_count"] = count
    smoke_record.summary = f"{count} MCP plugin(s) configured"


@pytest.mark.requires_auth
def test_tier2_vision_url_configured(
    auth_http_session, smoke_config, smoke_record
) -> None:
    """`/api/config/services` exposes a vision block; if URL set, pass; else skip."""
    url = smoke_config.url("webui", "/api/config/services")
    smoke_record.checked = f"GET {url} (auth) — vision.url"
    smoke_record.expected = "vision.url non-empty OR feature disabled"

    r = auth_http_session.get(url, timeout=smoke_config.timeouts["default"])
    body = _safe_json(r)
    smoke_record.actual = body

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    vision = (
        (body.get("vision") if isinstance(body, dict) else None)
        or (body.get("services", {}) or {}).get("vision", {})
        if isinstance(body, dict)
        else {}
    )
    vision_url = (vision or {}).get("url", "") if isinstance(vision, dict) else ""
    smoke_record.extras["vision_url"] = vision_url

    if not vision_url:
        smoke_record.summary = "Vision feature disabled by config"
        pytest.skip("vision.url is empty — feature optional")

    smoke_record.summary = f"Vision URL configured ({vision_url})"


# ─── API surface integrity ───────────────────────────────────────────────


def test_tier2_api_attitudes_loaded(
    http_session, smoke_config, smoke_record
) -> None:
    """`/api/attitudes` returns a non-empty collection."""
    url = smoke_config.url("api", "/api/attitudes")
    smoke_record.checked = f"GET {url}"
    smoke_record.expected = "200 + non-empty collection"

    r = http_session.get(url, timeout=smoke_config.timeouts["default"])
    body = _safe_json(r)
    smoke_record.actual = f"{r.status_code} ({_len_or_keys(body)})"

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    count = (
        len(body)
        if isinstance(body, (list, dict))
        else 0
    )
    assert count > 0, "attitudes collection is empty"
    smoke_record.extras["count"] = count
    smoke_record.summary = f"{count} attitude(s) loaded"


def test_tier2_api_emotion_state(
    http_session, smoke_config, smoke_record
) -> None:
    """`/api/emotion/state` returns PAD-shaped state."""
    url = smoke_config.url("api", "/api/emotion/state")
    smoke_record.checked = f"GET {url}"
    smoke_record.expected = "200 + dict with at least one PAD-style key"

    r = http_session.get(url, timeout=smoke_config.timeouts["default"])
    body = _safe_json(r)
    smoke_record.actual = body

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    assert isinstance(body, dict), f"expected dict, got {type(body).__name__}"
    pad_keys = {
        "pleasure", "arousal", "dominance",
        "p", "a", "d",
        "valence",  # alternate axis name some systems use
    }
    found = pad_keys.intersection(_lowercase_keys(body))
    assert found, f"no PAD-style keys in emotion state: {sorted(body.keys())[:10]}"
    smoke_record.extras["found_keys"] = sorted(found)
    smoke_record.summary = f"Emotion state alive (keys: {', '.join(sorted(found))})"


def test_tier2_api_semantic_status(
    http_session, smoke_config, smoke_record
) -> None:
    """`/api/semantic/status` returns 200 with valid JSON."""
    url = smoke_config.url("api", "/api/semantic/status")
    smoke_record.checked = f"GET {url}"
    smoke_record.expected = "200 + valid JSON"

    r = http_session.get(url, timeout=smoke_config.timeouts["default"])
    body = _safe_json(r)
    smoke_record.actual = body

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    assert isinstance(body, (dict, list)), "semantic status not JSON-shaped"
    smoke_record.summary = "Semantic index status endpoint responding"


# ─── Auth + logs ─────────────────────────────────────────────────────────


@pytest.mark.requires_auth
def test_tier2_auth_status_admin(
    auth_http_session, smoke_config, smoke_record
) -> None:
    """`/api/auth/status` reflects an authenticated admin session."""
    url = smoke_config.url("webui", "/api/auth/status")
    smoke_record.checked = f"GET {url} (auth)"
    smoke_record.expected = "authenticated=true; role consistent with smoke user"

    r = auth_http_session.get(url, timeout=smoke_config.timeouts["default"])
    body = _safe_json(r)
    smoke_record.actual = body

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    authed = body.get("authenticated") is True or body.get("logged_in") is True
    assert authed, f"session not reported as authenticated: {body}"
    smoke_record.summary = (
        f"Session authenticated as {body.get('username') or smoke_config.auth_username!r}"
    )


@pytest.mark.requires_auth
def test_tier2_log_groups_readable(
    auth_http_session, smoke_config, smoke_record
) -> None:
    """`/api/log_groups` returns a non-empty list (loguru registry alive)."""
    url = smoke_config.url("webui", "/api/log_groups")
    smoke_record.checked = f"GET {url} (auth)"
    smoke_record.expected = "200 + non-empty list"

    r = auth_http_session.get(url, timeout=smoke_config.timeouts["default"])
    body = _safe_json(r)
    smoke_record.actual = f"{r.status_code} ({_len_or_keys(body)})"

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    items = body if isinstance(body, list) else (
        body.get("groups") or body.get("data") or []
    )
    smoke_record.extras["group_count"] = len(items)
    assert items, "log_groups returned empty list"
    smoke_record.summary = f"{len(items)} log group(s) registered"


@pytest.mark.requires_auth
@pytest.mark.requires_log_endpoint
def test_tier2_no_recent_errors(
    auth_http_session, smoke_config, smoke_record, log_baseline
) -> None:
    """No CRITICAL / Traceback / unhandled-error lines in recent container logs."""
    url = smoke_config.url("webui", "/api/logs/tail")
    smoke_record.checked = (
        f"GET {url}?source=container "
        f"&lines={smoke_config.log_baseline_lookback_lines} (auth)"
    )
    smoke_record.expected = "0 ERROR/CRITICAL/Traceback lines since baseline"

    try:
        r = auth_http_session.get(
            url,
            params={
                "source": "container",
                "lines": smoke_config.log_baseline_lookback_lines,
            },
            timeout=smoke_config.timeouts["log_tail"],
        )
    except requests.RequestException as exc:
        smoke_record.actual = f"request error: {exc}"
        pytest.skip(f"log endpoint unreachable: {exc}")
        return

    if r.status_code == 500:
        smoke_record.actual = f"500 — docker socket likely not mounted"
        pytest.skip("/api/logs/tail returned 500 — log endpoint unavailable")
    assert r.status_code == 200, f"unexpected status {r.status_code}"

    body = _safe_json(r)
    text_block = body.get("text") or body.get("lines") or ""
    if isinstance(text_block, list):
        lines = [str(x) for x in text_block]
    else:
        lines = str(text_block).splitlines()

    pattern = re.compile(r"\b(CRITICAL|FATAL|Traceback|Unhandled exception)\b")
    bad = [ln for ln in lines if pattern.search(ln)]
    smoke_record.extras["scanned_lines"] = len(lines)
    smoke_record.extras["error_lines"] = bad

    smoke_record.actual = f"scanned {len(lines)} lines; {len(bad)} matched"
    if bad:
        smoke_record.summary = f"Found {len(bad)} error line(s) in container logs"
        # Surface the first 5 in the failure message for the renderer.
        snippet = "\n".join(bad[:5])
        pytest.fail(
            f"{len(bad)} error/Traceback lines in recent logs:\n{snippet}"
        )
    smoke_record.summary = f"No errors in last {len(lines)} log lines"


# ─── helpers ─────────────────────────────────────────────────────────────


def _safe_json(r: requests.Response):
    try:
        return r.json()
    except ValueError:
        return {"_raw": r.text[:500]}


def _len_or_keys(body) -> str:
    if isinstance(body, list):
        return f"list[{len(body)}]"
    if isinstance(body, dict):
        return f"dict({list(body.keys())[:6]})"
    return type(body).__name__


def _services_dict(body) -> dict[str, str]:
    """Extract a name->status map from /api/health/* response shapes."""
    if not isinstance(body, dict):
        return {}
    services = body.get("services")
    if isinstance(services, list):
        return {
            s.get("name"): s.get("status")
            for s in services
            if isinstance(s, dict) and s.get("name")
        }
    if isinstance(services, dict):
        return {k: v.get("status") if isinstance(v, dict) else v
                for k, v in services.items()}
    return {}


def _lowercase_keys(d: dict) -> set[str]:
    out: set[str] = set()
    for k in d:
        if isinstance(k, str):
            out.add(k.lower())
    return out
