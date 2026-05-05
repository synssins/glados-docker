"""Tier 1 — Health probes.

Cheapest possible "is the system alive" tests. All under 2 s individually,
designed to run in parallel. Only one test (`login_succeeds`) interacts
with auth; everything else uses the unauthenticated session.

Per TEST_PLAN.md §"Tier 1".
"""

from __future__ import annotations

import pytest
import requests

pytestmark = pytest.mark.tier1


def test_tier1_scheme_is_https(smoke_config, smoke_record) -> None:
    """Configured target is HTTPS (the prod expectation)."""
    smoke_record.checked = "config.yaml `scheme` value"
    smoke_record.expected = "https"
    smoke_record.actual = smoke_config.scheme

    if smoke_config.scheme == "http":
        smoke_record.summary = (
            "Target is plain HTTP — production expectation is HTTPS"
        )
        pytest.skip("HTTP target is allowed but not the prod state — see warning")

    smoke_record.summary = f"Target {smoke_config.host} is HTTPS as expected"


def test_tier1_login_succeeds(http_session, smoke_config, smoke_record) -> None:
    """WebUI accepts the configured smoke credentials and issues a session.

    Independent of the `auth_http_session` fixture so the failure mode
    here surfaces the actual transport / status error (TLS, 401, 502,
    etc.) instead of being collapsed to "None".
    """

    url = smoke_config.url("webui", "/login")
    smoke_record.checked = f"POST {url} with configured credentials"
    smoke_record.expected = "200/302/303 + Set-Cookie: glados_session=..."

    try:
        r = http_session.post(
            url,
            data={
                "username": smoke_config.auth_username,
                "password": smoke_config.auth_password,
            },
            timeout=smoke_config.timeouts["default"],
            allow_redirects=False,
        )
    except requests.exceptions.SSLError as exc:
        smoke_record.actual = f"TLS error: {exc}"
        smoke_record.summary = (
            "TLS verification failed — set verify_tls: false in "
            "config.yaml or point host at the cert's CN/SAN"
        )
        pytest.fail(f"TLS error reaching {url}: {exc}")
        return
    except requests.RequestException as exc:
        smoke_record.actual = f"transport error: {exc}"
        smoke_record.summary = (
            f"Could not reach {smoke_config.host}:{smoke_config.ports['webui']}"
        )
        pytest.fail(f"transport error: {exc}")
        return

    smoke_record.extras["status"] = r.status_code
    smoke_record.extras["set_cookie_present"] = "Set-Cookie" in r.headers

    if r.status_code == 401:
        smoke_record.actual = "401 — credentials rejected"
        smoke_record.summary = (
            "Bad credentials — check GLADOS_SMOKE_USER / GLADOS_SMOKE_PASS"
        )
        pytest.fail("401 from /login; credentials rejected")
        return

    if r.status_code not in (200, 302, 303):
        smoke_record.actual = f"{r.status_code} {r.text[:200]}"
        smoke_record.summary = f"Login returned unexpected status {r.status_code}"
        pytest.fail(f"unexpected /login status {r.status_code}")
        return

    cookie = r.cookies.get("glados_session")
    if not cookie:
        smoke_record.actual = (
            f"{r.status_code} but no glados_session cookie in response"
        )
        smoke_record.summary = "Login responded OK but no session cookie issued"
        pytest.fail("no glados_session cookie set by /login")
        return

    smoke_record.actual = f"{r.status_code} + glados_session cookie issued"
    smoke_record.summary = (
        f"Logged in as {smoke_config.auth_username!r} on {smoke_config.host}"
    )


def test_tier1_api_health_ok(http_session, smoke_config, smoke_record) -> None:
    """GLaDOS API /health returns 200 with engine running."""
    url = smoke_config.url("api", "/health")
    smoke_record.checked = f"GET {url}"
    smoke_record.expected = '200 + {"status": "ok", "engine": "running"}'

    r = http_session.get(url, timeout=smoke_config.timeouts["default"])
    body = _safe_json(r)
    smoke_record.actual = f"{r.status_code} {body}"

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    assert body.get("status") == "ok", f"status field is {body.get('status')!r}"
    assert body.get("engine") == "running", f"engine field is {body.get('engine')!r}"
    smoke_record.summary = (
        f"API on :{smoke_config.ports['api']} reports engine running"
    )


def test_tier1_webui_health_ok(http_session, smoke_config, smoke_record) -> None:
    """WebUI /health returns 200."""
    url = smoke_config.url("webui", "/health")
    smoke_record.checked = f"GET {url}"
    smoke_record.expected = "200"

    r = http_session.get(url, timeout=smoke_config.timeouts["default"])
    smoke_record.actual = str(r.status_code)

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    smoke_record.summary = f"WebUI on :{smoke_config.ports['webui']} healthy"


def test_tier1_webui_health_public_ok(
    http_session, smoke_config, smoke_record
) -> None:
    """`/api/health/public` reports all four services ok."""
    url = smoke_config.url("webui", "/api/health/public")
    smoke_record.checked = f"GET {url} (probes API/TTS/STT/HA)"
    smoke_record.expected = "200 + every service status='ok'"

    r = http_session.get(url, timeout=smoke_config.timeouts["ha_aggregate"])
    body = _safe_json(r)
    smoke_record.actual = f"{r.status_code} {body}"

    assert r.status_code == 200, f"unexpected status {r.status_code}"
    services = body.get("services") or []
    assert services, "services list empty or missing"

    down = [s["name"] for s in services if s.get("status") != "ok"]
    smoke_record.extras["services"] = services
    if down:
        smoke_record.summary = f"Down: {', '.join(down)}"
        pytest.fail(f"these services report not-ok: {down}")
    smoke_record.summary = (
        f"All {len(services)} services ok ({', '.join(s['name'] for s in services)})"
    )


def test_tier1_audio_server_responding(
    http_session, smoke_config, smoke_record, tcp_check
) -> None:
    """Port 5051 (HA audio file server) accepts TCP and returns *some* HTTP response."""
    url = smoke_config.url("audio", "/")
    smoke_record.checked = f"GET {url} (HA audio file server)"
    smoke_record.expected = "any HTTP response (200/403/404)"

    # TCP first — pure listener check.
    if not tcp_check(smoke_config.host, smoke_config.ports["audio"], timeout=2.0):
        smoke_record.actual = "TCP connect failed"
        smoke_record.summary = "Audio file server not accepting connections"
        pytest.fail(f"TCP connect to {smoke_config.host}:{smoke_config.ports['audio']} failed")

    try:
        r = http_session.get(url, timeout=smoke_config.timeouts["default"])
        smoke_record.actual = str(r.status_code)
    except requests.RequestException as exc:
        smoke_record.actual = f"request error: {exc}"
        pytest.fail(f"HTTP request failed: {exc}")
        return  # for type checkers

    assert r.status_code in (200, 301, 302, 403, 404), (
        f"unexpected status {r.status_code} from audio server"
    )
    smoke_record.summary = (
        f"Audio file server on :{smoke_config.ports['audio']} responded {r.status_code}"
    )


def test_tier1_log_baseline_capture(log_baseline, smoke_record) -> None:
    """Capture the log-diff baseline timestamp for Tier 2 to use."""
    smoke_record.checked = "session-start log baseline"
    smoke_record.expected = "ISO timestamp captured"
    smoke_record.actual = log_baseline["captured_at_iso"]
    smoke_record.extras["captured_at_iso"] = log_baseline["captured_at_iso"]
    smoke_record.summary = (
        f"Log diff baseline anchored at {log_baseline['captured_at_iso']}"
    )

    assert log_baseline["captured_at"] > 0


# ─── helpers ─────────────────────────────────────────────────────────────


def _safe_json(r: requests.Response) -> dict:
    try:
        body = r.json()
        return body if isinstance(body, dict) else {"_raw": body}
    except ValueError:
        return {"_raw": r.text[:500]}
