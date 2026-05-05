"""Shared fixtures and pytest plumbing for the GLaDOS smoke suite.

This module:
- loads tests/smoke/config.yaml (with env overrides),
- provides HTTP session fixtures (unauthenticated and authenticated),
- exposes a `smoke_record` fixture so each test can populate the JSON
  report's `summary` / `details` / `extras` fields,
- maps every test function to its stable smoke ID
  (`test_tier1_api_health_ok` -> `tier1::api_health_ok`),
- enforces operator-controlled `disabled_tests` from config.yaml,
- skips `requires_auth` tests when login fails,
- skips `requires_audio_fixtures` tests when the WAVs are missing.

The custom JSON reporter (_reporter.py) consumes the per-test records
captured here. The reporter is registered as a plugin via `pytest_plugins`
so a bare `pytest tests/smoke` works without extra flags.
"""

from __future__ import annotations

import os
import re
import socket
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests
import urllib3
import yaml

# Register the reporter plugin so `pytest tests/smoke` activates it
# without requiring `-p tests.smoke._reporter`.
pytest_plugins = ["tests.smoke._reporter"]


SMOKE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SMOKE_DIR / "config.yaml"
LOCAL_CONFIG_PATH = SMOKE_DIR / "config.local.yaml"


# ─── Config loading ───────────────────────────────────────────────────────


@dataclass
class SmokeConfig:
    raw: dict[str, Any]
    host: str
    scheme: str
    verify_tls: bool
    ports: dict[str, int]
    auth_username: str
    auth_password: str
    timeouts: dict[str, float]
    fixtures_dir: Path
    sentinel_utterance: str
    log_severity_threshold: str
    log_baseline_lookback_lines: int
    regression: dict[str, Any]
    disabled_tests: set[str]
    reports_dir: Path
    reports_keep: int

    def url(self, port_key: str, path: str = "") -> str:
        port = self.ports[port_key]
        if not path.startswith("/"):
            path = "/" + path if path else ""
        return f"{self.scheme}://{self.host}:{port}{path}"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge `overlay` into `base`. Overlay scalars/lists replace
    base; overlay dicts merge recursively. Returns a new dict."""
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_config() -> SmokeConfig:
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Smoke config not found at {CONFIG_PATH}. "
            "Did you delete it?"
        )
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}

    # Operator-supplied overlay (gitignored). Anything in here wins.
    if LOCAL_CONFIG_PATH.exists():
        overlay = yaml.safe_load(LOCAL_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        if isinstance(overlay, dict):
            raw = _deep_merge(raw, overlay)

    # Host override via env
    host = os.environ.get("GLADOS_SMOKE_HOST", raw.get("host", "127.0.0.1"))

    # TLS verification override
    verify_env = os.environ.get("GLADOS_SMOKE_INSECURE", "").strip()
    verify_tls = bool(raw.get("verify_tls", True))
    if verify_env in {"1", "true", "yes"}:
        verify_tls = False

    auth_block = raw.get("auth", {}) or {}
    user_env = auth_block.get("username_env", "GLADOS_SMOKE_USER")
    pass_env = auth_block.get("password_env", "GLADOS_SMOKE_PASS")
    username = os.environ.get(user_env) or auth_block.get("default_username", "admin")
    password = os.environ.get(pass_env) or auth_block.get("default_password", "glados")

    fixtures_dir_raw = (raw.get("fixtures") or {}).get("audio_dir", "fixtures")
    fixtures_dir = SMOKE_DIR / fixtures_dir_raw

    return SmokeConfig(
        raw=raw,
        host=host,
        scheme=raw.get("scheme", "https"),
        verify_tls=verify_tls,
        ports=raw.get("ports", {"api": 8015, "webui": 8052, "audio": 5051}),
        auth_username=username,
        auth_password=password,
        timeouts={
            "default": float((raw.get("timeouts") or {}).get("default_request_s", 5)),
            "tts": float((raw.get("timeouts") or {}).get("tts_synth_s", 8)),
            "log_tail": float((raw.get("timeouts") or {}).get("log_tail_s", 10)),
            "ha_aggregate": float((raw.get("timeouts") or {}).get("ha_aggregate_s", 6)),
        },
        fixtures_dir=fixtures_dir,
        sentinel_utterance=(raw.get("sentinel") or {}).get(
            "utterance", "what time is it"
        ),
        log_severity_threshold=raw.get("log_severity_threshold", "ERROR"),
        log_baseline_lookback_lines=int(raw.get("log_baseline_lookback_lines", 200)),
        regression=raw.get("regression") or {},
        disabled_tests=set(raw.get("disabled_tests") or []),
        reports_dir=SMOKE_DIR / "reports",
        reports_keep=int(raw.get("reports_keep", 30)),
    )


# ─── Test ID mapping ──────────────────────────────────────────────────────


_TIER_RE = re.compile(r"^test_(tier\d+)_(.+)$")


def smoke_id_for_nodeid(nodeid: str) -> str | None:
    """Convert a pytest nodeid into a stable smoke ID.

    `tests/smoke/test_tier1_health.py::test_tier1_api_health_ok` ->
    `tier1::api_health_ok`. Returns None if the function name doesn't
    match the convention.
    """
    funcname = nodeid.rsplit("::", 1)[-1]
    # Drop pytest parametrize suffix `[...]` if present.
    funcname = funcname.split("[", 1)[0]
    m = _TIER_RE.match(funcname)
    if not m:
        return None
    return f"{m.group(1)}::{m.group(2)}"


# ─── Per-test record store (consumed by _reporter.py) ─────────────────────


@dataclass
class SmokeRecord:
    """Mutable record a test populates so the JSON reporter can pick it up."""

    smoke_id: str
    name: str = ""
    summary: str = ""
    checked: str = ""
    expected: Any = None
    actual: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


def _records_store(session: pytest.Session) -> dict[str, SmokeRecord]:
    store = getattr(session, "_smoke_records", None)
    if store is None:
        store = {}
        session._smoke_records = store  # type: ignore[attr-defined]
    return store


# ─── Pytest hooks ─────────────────────────────────────────────────────────


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--include-mutating",
        action="store_true",
        default=False,
        help="Include tests marked `mutates` (write to GLaDOS state).",
    )
    parser.addoption(
        "--capture-baseline",
        action="store_true",
        default=False,
        help="Tier 4: capture a fresh regression baseline.",
    )
    parser.addoption(
        "--baseline",
        action="store",
        default=None,
        help="Tier 4: path to the baseline directory to compare against.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "tier1: Tier 1 health probes")
    config.addinivalue_line("markers", "tier2: Tier 2 component reachability")
    config.addinivalue_line("markers", "tier3: Tier 3 end-to-end")
    config.addinivalue_line("markers", "tier4: Tier 4 regression diff")
    config.addinivalue_line("markers", "regression: Tier 4 regression alias")
    config.addinivalue_line("markers", "slow: takes more than 10 s")
    config.addinivalue_line(
        "markers", "requires_audio_fixtures: skipped if WAV fixtures missing"
    )
    config.addinivalue_line(
        "markers", "requires_auth: skipped if WebUI login failed at suite start"
    )
    config.addinivalue_line(
        "markers", "requires_log_endpoint: skipped if /api/logs/tail returned 500"
    )
    config.addinivalue_line(
        "markers",
        "mutates: writes to GLaDOS state; opt-in only via --include-mutating",
    )

    # Suppress urllib3 warnings when verify_tls is off.
    cfg = _load_config()
    config._smoke_cfg = cfg  # type: ignore[attr-defined]
    if not cfg.verify_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    cfg: SmokeConfig = config._smoke_cfg  # type: ignore[attr-defined]
    include_mutating = config.getoption("--include-mutating")

    skip_disabled = pytest.mark.skip(reason="disabled in config.yaml")
    skip_mutating = pytest.mark.skip(
        reason="mutates GLaDOS state; opt-in via --include-mutating"
    )

    for item in items:
        sid = smoke_id_for_nodeid(item.nodeid)
        if sid and sid in cfg.disabled_tests:
            item.add_marker(skip_disabled)
            continue
        if "mutates" in item.keywords and not include_mutating:
            item.add_marker(skip_mutating)


# ─── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def smoke_config(pytestconfig: pytest.Config) -> SmokeConfig:
    return pytestconfig._smoke_cfg  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
def http_session(smoke_config: SmokeConfig) -> requests.Session:
    """Unauthenticated HTTP session with sensible defaults."""

    if smoke_config.scheme == "http":
        warnings.warn(
            f"Smoke target is plain HTTP ({smoke_config.host}); "
            "production expectation is HTTPS.",
            stacklevel=1,
        )

    s = requests.Session()
    s.verify = smoke_config.verify_tls
    s.headers.update({"User-Agent": "glados-smoke/1.0"})
    return s


@pytest.fixture(scope="session")
def auth_http_session(
    smoke_config: SmokeConfig, http_session: requests.Session
) -> requests.Session | None:
    """Logged-in HTTP session, or None if login failed.

    Tests that depend on authentication declare `requires_auth`; those
    are skipped automatically when this fixture is None.
    """

    s = requests.Session()
    s.verify = smoke_config.verify_tls
    s.headers.update({"User-Agent": "glados-smoke/1.0"})

    login_url = smoke_config.url("webui", "/login")
    try:
        r = s.post(
            login_url,
            data={
                "username": smoke_config.auth_username,
                "password": smoke_config.auth_password,
            },
            timeout=smoke_config.timeouts["default"],
            allow_redirects=False,
        )
    except requests.RequestException:
        return None

    if r.status_code not in (200, 302, 303):
        return None
    if "glados_session" not in s.cookies:
        return None
    return s


@pytest.fixture(autouse=True)
def _skip_if_no_auth(request: pytest.FixtureRequest) -> None:
    """Skip tests marked `requires_auth` when login failed."""

    if "requires_auth" not in request.node.keywords:
        return
    auth = request.getfixturevalue("auth_http_session")
    if auth is None:
        pytest.skip("authentication unavailable (login failed at suite start)")


@pytest.fixture(scope="session")
def log_baseline(smoke_config: SmokeConfig) -> dict[str, Any]:
    """Capture a baseline timestamp + log snapshot at suite start.

    Tier 2's `no_recent_errors` test diffs against this. The
    `before_lines` field can be used by future log-diff features; the
    timestamp is the canonical anchor.
    """

    return {
        "captured_at": time.time(),
        "captured_at_iso": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
        "before_lines": [],
    }


@pytest.fixture
def audio_fixture(smoke_config: SmokeConfig):
    """Return a callable that loads a WAV by name or skips if missing."""

    def _load(name: str) -> bytes:
        path = smoke_config.fixtures_dir / name
        if not path.exists():
            pytest.skip(
                f"audio fixture missing: {path.relative_to(SMOKE_DIR)}. "
                "Record it per fixtures/README.md."
            )
        return path.read_bytes()

    return _load


@pytest.fixture
def smoke_record(request: pytest.FixtureRequest) -> SmokeRecord:
    """Mutable record the reporter reads back when serializing the run.

    Each test populates this — `summary` is one line, `checked` is what
    was probed, `expected`/`actual` describe the assertion, and
    `extras` carries anything else (e.g. a transcript on Tier 3).
    """

    sid = smoke_id_for_nodeid(request.node.nodeid) or request.node.name
    rec = SmokeRecord(smoke_id=sid, name=request.node.name)
    _records_store(request.session)[request.node.nodeid] = rec
    return rec


@pytest.fixture(scope="session")
def tcp_check():
    """Lightweight TCP connect helper used by health probes."""

    def _check(host: str, port: int, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    return _check


# ─── Convenience helpers exposed to tests ────────────────────────────────


def host_only(url: str) -> str:
    """Strip scheme + path so we can show a clean target in the report."""

    parts = urlsplit(url)
    return parts.netloc or url
