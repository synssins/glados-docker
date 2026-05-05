"""Diff helper for the HTML renderer.

Given a current and a previous JSON report, classify each test into one
of: unchanged_pass / unchanged_fail / unchanged_skip / regressed /
recovered / new / removed / flaky_candidate. Matching is by stable
`id` field, NOT display name.

Returned shape is consumed directly by `_render_report.py`.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

# Categories
UNCHANGED_PASS = "unchanged_pass"
UNCHANGED_FAIL = "unchanged_fail"
UNCHANGED_SKIP = "unchanged_skip"
REGRESSED = "regressed"
RECOVERED = "recovered"
NEW = "new"
REMOVED = "removed"
FLAKY_CANDIDATE = "flaky_candidate"


def compute_diff(
    current: dict[str, Any], previous: dict[str, Any] | None
) -> dict[str, Any]:
    """Return a structured diff between two JSON reports.

    If `previous` is None, the diff has no per-test entries — the
    caller should hide the diff banner entirely.

    Output:

        {
            "previous_run_id": str | None,
            "previous_started_at": str | None,
            "host_changed": bool,
            "previous_host": str | None,
            "current_host": str,
            "commit_changed": bool,
            "previous_commit": str | None,
            "current_commit": str,
            "stale": bool,
            "stale_age_days": int | None,
            "regressed": [test_id, ...],
            "recovered": [test_id, ...],
            "new": [test_id, ...],
            "removed": [test_id, ...],
            "flaky": [test_id, ...],
            "by_id": {test_id: category, ...},
        }
    """

    if previous is None:
        return {
            "previous_run_id": None,
            "previous_started_at": None,
            "host_changed": False,
            "previous_host": None,
            "current_host": current.get("target_host"),
            "commit_changed": False,
            "previous_commit": None,
            "current_commit": current.get("git_commit"),
            "stale": False,
            "stale_age_days": None,
            "regressed": [],
            "recovered": [],
            "new": [],
            "removed": [],
            "flaky": [],
            "by_id": {},
        }

    cur_tests = _flatten_tests(current)
    prev_tests = _flatten_tests(previous)
    all_ids = set(cur_tests) | set(prev_tests)

    by_id: dict[str, str] = {}
    for tid in all_ids:
        cur = cur_tests.get(tid)
        prev = prev_tests.get(tid)
        by_id[tid] = _classify(cur, prev)

    regressed = sorted(t for t, c in by_id.items() if c == REGRESSED)
    recovered = sorted(t for t, c in by_id.items() if c == RECOVERED)
    new = sorted(t for t, c in by_id.items() if c == NEW)
    removed = sorted(t for t, c in by_id.items() if c == REMOVED)
    flaky = sorted(t for t, c in by_id.items() if c == FLAKY_CANDIDATE)

    prev_host = previous.get("target_host")
    cur_host = current.get("target_host")
    prev_commit = previous.get("git_commit")
    cur_commit = current.get("git_commit")

    age_days, stale = _age(previous.get("started_at"))

    return {
        "previous_run_id": previous.get("run_id"),
        "previous_started_at": previous.get("started_at"),
        "host_changed": bool(prev_host) and prev_host != cur_host,
        "previous_host": prev_host,
        "current_host": cur_host,
        "commit_changed": bool(prev_commit) and prev_commit != cur_commit,
        "previous_commit": prev_commit,
        "current_commit": cur_commit,
        "stale": stale,
        "stale_age_days": age_days,
        "regressed": regressed,
        "recovered": recovered,
        "new": new,
        "removed": removed,
        "flaky": flaky,
        "by_id": by_id,
    }


# ─── helpers ─────────────────────────────────────────────────────────────


def _flatten_tests(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for tier in report.get("tiers", []):
        for t in tier.get("tests", []):
            tid = t.get("id")
            if not tid:
                continue
            out[tid] = t
    return out


def _classify(
    cur: dict[str, Any] | None, prev: dict[str, Any] | None
) -> str:
    if cur is None and prev is None:
        return UNCHANGED_PASS  # impossible, but sane default
    if cur is None:
        return REMOVED
    if prev is None:
        return NEW

    cs = cur.get("status")
    ps = prev.get("status")

    if cs == "PASS" and ps in ("FAIL", "ERROR"):
        return RECOVERED
    if cs in ("FAIL", "ERROR") and ps == "PASS":
        return REGRESSED

    # Same-status flaky-candidate detection: >3x AND >1s absolute.
    if cs == ps:
        cur_d = float(cur.get("duration_sec") or 0)
        prev_d = float(prev.get("duration_sec") or 0)
        if prev_d > 0 and cur_d > 0:
            ratio = max(cur_d, prev_d) / max(min(cur_d, prev_d), 1e-6)
            absolute = abs(cur_d - prev_d)
            if ratio > 3.0 and absolute > 1.0:
                return FLAKY_CANDIDATE

    if cs == "PASS":
        return UNCHANGED_PASS
    if cs == "SKIP":
        return UNCHANGED_SKIP
    return UNCHANGED_FAIL


def _age(started_at: str | None) -> tuple[int | None, bool]:
    if not started_at:
        return None, False
    try:
        dt = _dt.datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except ValueError:
        return None, False
    now = _dt.datetime.now(_dt.timezone.utc)
    age = (now - dt).days
    return age, age >= 30
