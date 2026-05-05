"""Pytest plugin that emits a structured JSON report for the smoke suite.

The runner pipes the JSON to `_render_report.py` which produces the HTML
the operator actually reads. Tests populate per-test details via the
`smoke_record` fixture in `conftest.py`; this plugin reads that store on
`pytest_sessionfinish` and writes
`tests/smoke/reports/smoke-<UTC-timestamp>.json`.

Side effects on session finish:
- one-line console summary
- prune older reports beyond `reports_keep`
- best-effort fetch of `/api/logs/tail` on failures, capped at 20 lines
"""

from __future__ import annotations

import json
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.smoke.conftest import (
    SMOKE_DIR,
    SmokeConfig,
    SmokeRecord,
    smoke_id_for_nodeid,
)

__version__ = "1.0.0"


# ─── In-memory report state ───────────────────────────────────────────────


@dataclass
class TestResult:
    nodeid: str
    smoke_id: str
    name: str
    tier: str
    status: str = "PENDING"  # PASS | FAIL | SKIP | ERROR
    duration_sec: float = 0.0
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    logs: list[str] | None = None
    error: dict[str, str] | None = None


def _tier_for_smoke_id(smoke_id: str) -> str:
    if "::" in smoke_id:
        return smoke_id.split("::", 1)[0]
    return "other"


def _git_commit() -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(SMOKE_DIR.parents[1]), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown"


# ─── Plugin hooks ─────────────────────────────────────────────────────────


def pytest_sessionstart(session: pytest.Session) -> None:
    cfg: SmokeConfig = session.config._smoke_cfg  # type: ignore[attr-defined]
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)

    state = _ReporterState(
        cfg=cfg,
        run_id=time.strftime("%Y%m%d-%H%M%S", time.gmtime()),
        started_at=time.time(),
        target_host=cfg.host,
        git_commit=_git_commit(),
    )
    session._smoke_reporter = state  # type: ignore[attr-defined]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Any:
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" and not (
        report.when == "setup" and report.skipped
    ):
        return

    state: _ReporterState = item.session._smoke_reporter  # type: ignore[attr-defined]
    sid = smoke_id_for_nodeid(item.nodeid) or item.name
    tier = _tier_for_smoke_id(sid)

    # Decide status.
    if report.skipped:
        status = "SKIP"
    elif report.passed:
        status = "PASS"
    elif report.failed:
        status = "FAIL" if call.when == "call" else "ERROR"
    else:
        status = "ERROR"

    # Read the per-test record the test populated via smoke_record fixture.
    records: dict[str, SmokeRecord] = getattr(item.session, "_smoke_records", {})
    rec = records.get(item.nodeid)

    summary = (rec.summary if rec else "") or _default_summary(status, item.name)
    details = {}
    if rec:
        details["checked"] = rec.checked
        details["expected"] = rec.expected
        details["actual"] = rec.actual
        if rec.extras:
            details["extras"] = rec.extras

    error = None
    if status in ("FAIL", "ERROR"):
        message = str(report.longrepr) if report.longrepr else ""
        # Truncate massive tracebacks. Most diagnostic value is in the
        # final assertion line and the few frames around it; the deep
        # urllib3/ssl frames don't help and bloat the HTML report.
        message = _trim_traceback(message, max_chars=2000)
        error = {"message": message, "type": status}

    logs = None
    if status in ("FAIL", "ERROR"):
        logs = _best_effort_log_tail(state, item.session)

    result = TestResult(
        nodeid=item.nodeid,
        smoke_id=sid,
        name=item.name,
        tier=tier,
        status=status,
        duration_sec=round(report.duration, 3),
        summary=summary,
        details=details,
        logs=logs,
        error=error,
    )
    state.results.append(result)


def pytest_sessionfinish(
    session: pytest.Session, exitstatus: int
) -> None:
    state: _ReporterState | None = getattr(session, "_smoke_reporter", None)
    if state is None:
        return

    state.finished_at = time.time()
    payload = state.build_payload()
    out_path = state.cfg.reports_dir / f"smoke-{state.run_id}.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Prune older reports (never touches _examples/ or baselines/).
    _prune_reports(state.cfg)

    # Console summary.
    summary = payload["summary"]
    target = state.target_host
    duration = payload["duration_sec"]
    line = (
        f"GLaDOS Smoke: {summary['passed']}/{summary['total']} PASS "
        f"({summary['skipped']} skip, {summary['failed']} fail) in {duration}s "
        f"on {target} — see {out_path.name}"
    )
    print()
    print(line)


# ─── Internals ────────────────────────────────────────────────────────────


@dataclass
class _ReporterState:
    cfg: SmokeConfig
    run_id: str
    started_at: float
    target_host: str
    git_commit: str
    finished_at: float = 0.0
    results: list[TestResult] = field(default_factory=list)

    def build_payload(self) -> dict[str, Any]:
        # Group results by tier in a deterministic order.
        tier_order = ["tier1", "tier2", "tier3", "tier4", "other"]
        groups: dict[str, list[TestResult]] = {t: [] for t in tier_order}
        for r in self.results:
            groups.setdefault(r.tier, []).append(r)

        tiers = []
        total_pass = total_fail = total_skip = total_err = 0
        for tname in tier_order:
            entries = groups.get(tname) or []
            if not entries:
                continue
            tier_pass = sum(1 for r in entries if r.status == "PASS")
            tier_fail = sum(1 for r in entries if r.status == "FAIL")
            tier_skip = sum(1 for r in entries if r.status == "SKIP")
            tier_err = sum(1 for r in entries if r.status == "ERROR")
            total_pass += tier_pass
            total_fail += tier_fail
            total_skip += tier_skip
            total_err += tier_err

            if tier_fail or tier_err:
                tier_status = "FAIL"
            elif tier_pass and not (tier_skip and not tier_pass):
                tier_status = "PASS"
            elif tier_skip and not tier_pass:
                tier_status = "SKIP"
            else:
                tier_status = "PASS"

            tier_duration = round(sum(r.duration_sec for r in entries), 2)

            tiers.append(
                {
                    "name": tname,
                    "status": tier_status,
                    "duration_sec": tier_duration,
                    "tests": [self._test_to_dict(r) for r in entries],
                }
            )

        total = total_pass + total_fail + total_skip + total_err
        if total_fail or total_err:
            overall = "FAIL"
        elif total_skip and not total_pass:
            overall = "SKIP"
        elif total_pass:
            overall = "PASS"
        else:
            overall = "EMPTY"

        duration = round((self.finished_at - self.started_at), 2)

        return {
            "run_id": self.run_id,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "duration_sec": duration,
            "target_host": self.target_host,
            "scheme": self.cfg.scheme,
            "git_commit": self.git_commit,
            "suite_version": __version__,
            "summary": {
                "total": total,
                "passed": total_pass,
                "failed": total_fail,
                "skipped": total_skip,
                "errors": total_err,
                "overall": overall,
            },
            "tiers": tiers,
        }

    @staticmethod
    def _test_to_dict(r: TestResult) -> dict[str, Any]:
        return {
            "id": r.smoke_id,
            "name": r.name,
            "status": r.status,
            "duration_sec": r.duration_sec,
            "summary": r.summary,
            "details": r.details,
            "logs": r.logs,
            "error": r.error,
        }


def _trim_traceback(message: str, max_chars: int = 2000) -> str:
    """Keep the head and tail of a long traceback so the renderer can
    show the call site AND the final exception line without bloating
    the HTML report."""
    if len(message) <= max_chars:
        return message
    head = max_chars // 3
    tail = max_chars - head
    return (
        message[:head]
        + "\n... (truncated; tail follows) ...\n"
        + message[-tail:]
    )


def _iso(ts: float) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _default_summary(status: str, name: str) -> str:
    if status == "PASS":
        return f"{name}: ok"
    if status == "SKIP":
        return f"{name}: skipped"
    if status == "ERROR":
        return f"{name}: errored"
    return f"{name}: failed"


def _best_effort_log_tail(
    state: _ReporterState, session: pytest.Session
) -> list[str] | None:
    """Try to grab the last 20 container log lines via /api/logs/tail.

    Returns None on any failure (no auth, endpoint unavailable, etc.).
    """

    auth = getattr(session, "_smoke_auth", None)
    if auth is None:
        # auth_http_session may not have been requested by any test yet.
        return None
    try:
        r = auth.get(
            f"{state.cfg.scheme}://{state.target_host}:"
            f"{state.cfg.ports['webui']}/api/logs/tail",
            params={"source": "container", "lines": 20},
            timeout=state.cfg.timeouts["log_tail"],
        )
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    text = data.get("text") or data.get("lines") or ""
    if isinstance(text, list):
        return [str(x) for x in text[-20:]]
    if isinstance(text, str):
        return text.splitlines()[-20:]
    return None


def _prune_reports(cfg: SmokeConfig) -> None:
    if cfg.reports_keep <= 0:
        return
    files = sorted(
        [
            p
            for p in cfg.reports_dir.glob("smoke-*.json")
            if p.is_file()
        ],
        key=lambda p: p.stat().st_mtime,
    )
    excess = len(files) - cfg.reports_keep
    if excess <= 0:
        return
    for old in files[:excess]:
        try:
            old.unlink()
        except OSError:
            pass
