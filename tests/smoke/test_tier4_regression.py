"""Tier 4 — Regression diff.

Two modes, both opt-in (mutating):

- `baseline_capture` runs each prompt in `config.regression.prompts` and
  saves the response + metadata under `tests/smoke/baselines/<UTC>/`.

- `baseline_compare` runs the same prompts and asserts structural
  equivalence vs a baseline directory passed via `--baseline=<dir>`:
  same model name in slot used, latency within `latency_factor` of
  baseline, response length within `length_band`. Exact text equality
  is NOT required — LLMs are non-deterministic.

Per TEST_PLAN.md §"Tier 4".
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.tier4, pytest.mark.regression]


@pytest.mark.mutates
@pytest.mark.slow
def test_tier4_baseline_capture(
    http_session, smoke_config, smoke_record, pytestconfig
) -> None:
    """Capture a fresh baseline for the configured prompt set.

    Run with: `pytest tests/smoke -m regression --include-mutating
                          --capture-baseline`.
    """
    if not pytestconfig.getoption("--capture-baseline"):
        smoke_record.summary = "capture mode not requested"
        pytest.skip("--capture-baseline not set")

    prompts = (smoke_config.regression or {}).get("prompts") or []
    if not prompts:
        smoke_record.summary = "no prompts configured"
        pytest.skip("config.yaml regression.prompts is empty")

    out_dir = (
        Path(__file__).parent
        / "baselines"
        / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    smoke_record.checked = f"capture {len(prompts)} prompt(s) into {out_dir.name}"
    smoke_record.expected = "all prompts produce non-empty responses"

    captured = []
    for entry in prompts:
        pid = entry.get("id")
        prompt = entry.get("prompt")
        if not (pid and prompt):
            continue
        record = _run_prompt(http_session, smoke_config, prompt)
        record["id"] = pid
        record["prompt"] = prompt
        captured.append(record)
        (out_dir / f"{pid}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )

    meta = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": smoke_config.host,
        "scheme": smoke_config.scheme,
        "prompt_count": len(captured),
    }
    (out_dir / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    failed = [r for r in captured if not r.get("response")]
    smoke_record.extras["captured"] = [
        {"id": r["id"], "len": len(r.get("response", "") or "")} for r in captured
    ]
    smoke_record.extras["baseline_dir"] = str(out_dir)
    smoke_record.actual = (
        f"{len(captured)} prompts captured to {out_dir.name}; "
        f"{len(failed)} empty"
    )
    assert not failed, f"{len(failed)} prompt(s) returned empty"
    smoke_record.summary = (
        f"Baseline captured: {len(captured)} prompts -> {out_dir.relative_to(out_dir.parents[1])}"
    )


@pytest.mark.mutates
@pytest.mark.slow
def test_tier4_baseline_compare(
    http_session, smoke_config, smoke_record, pytestconfig
) -> None:
    """Compare current responses against a captured baseline.

    Run with: `pytest tests/smoke -m regression --include-mutating
                          --baseline=tests/smoke/baselines/<dir>`.
    """
    baseline_arg = pytestconfig.getoption("--baseline")
    if not baseline_arg:
        smoke_record.summary = "no baseline path provided"
        pytest.skip("--baseline=<dir> not set")

    baseline_dir = Path(baseline_arg)
    if not baseline_dir.is_absolute():
        baseline_dir = (Path.cwd() / baseline_dir).resolve()
    if not baseline_dir.is_dir():
        smoke_record.summary = f"baseline missing at {baseline_dir}"
        pytest.fail(f"baseline directory not found: {baseline_dir}")

    smoke_record.checked = f"compare current run against {baseline_dir.name}"

    prompts = (smoke_config.regression or {}).get("prompts") or []
    if not prompts:
        smoke_record.summary = "no prompts configured"
        pytest.skip("config.yaml regression.prompts is empty")

    latency_factor = float(
        (smoke_config.regression or {}).get("latency_factor", 2.0)
    )
    length_band = float((smoke_config.regression or {}).get("length_band", 0.5))

    smoke_record.expected = (
        f"each prompt: response non-empty, latency <= {latency_factor}x baseline, "
        f"length within +/- {int(length_band*100)}%"
    )

    issues: list[str] = []
    diffs: list[dict[str, Any]] = []
    for entry in prompts:
        pid = entry.get("id")
        prompt = entry.get("prompt")
        if not (pid and prompt):
            continue
        baseline_path = baseline_dir / f"{pid}.json"
        if not baseline_path.exists():
            issues.append(f"{pid}: no baseline file")
            continue
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        current = _run_prompt(http_session, smoke_config, prompt)

        issue = _compare(
            pid=pid,
            baseline=baseline,
            current=current,
            latency_factor=latency_factor,
            length_band=length_band,
        )
        diffs.append(
            {
                "id": pid,
                "baseline_len": len(baseline.get("response", "") or ""),
                "current_len": len(current.get("response", "") or ""),
                "baseline_ms": baseline.get("latency_ms"),
                "current_ms": current.get("latency_ms"),
                "issue": issue,
            }
        )
        if issue:
            issues.append(f"{pid}: {issue}")

    smoke_record.extras["diffs"] = diffs
    smoke_record.actual = f"{len(diffs)} prompt(s) compared; {len(issues)} issue(s)"
    if issues:
        smoke_record.summary = "Regression diff found issues"
        pytest.fail("regression diff:\n" + "\n".join(issues))
    smoke_record.summary = (
        f"All {len(diffs)} prompts within tolerances vs {baseline_dir.name}"
    )


# ─── helpers ─────────────────────────────────────────────────────────────


def _run_prompt(http_session, smoke_config, prompt: str) -> dict[str, Any]:
    chat_url = smoke_config.url("api", "/v1/chat/completions")
    t0 = time.time()
    r = http_session.post(
        chat_url,
        json={
            "model": "glados",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 128,
            "temperature": 0.2,  # NEVER 0.0 — see operator memory.
            "stream": False,
        },
        timeout=30,
    )
    latency_ms = int((time.time() - t0) * 1000)
    response_text = ""
    model_used = ""
    if r.status_code == 200:
        body = r.json()
        choices = body.get("choices") or []
        if choices:
            response_text = (
                (choices[0].get("message") or {}).get("content")
                or choices[0].get("text")
                or ""
            ).strip()
        model_used = body.get("model") or ""
    return {
        "status": r.status_code,
        "response": response_text,
        "model": model_used,
        "latency_ms": latency_ms,
    }


def _compare(
    *,
    pid: str,
    baseline: dict[str, Any],
    current: dict[str, Any],
    latency_factor: float,
    length_band: float,
) -> str | None:
    if not current.get("response"):
        return "empty response"
    if (current.get("model") or "") and (baseline.get("model") or ""):
        if current["model"] != baseline["model"]:
            return f"model changed {baseline['model']!r} -> {current['model']!r}"
    bl_ms = baseline.get("latency_ms") or 0
    cur_ms = current.get("latency_ms") or 0
    if bl_ms and cur_ms > bl_ms * latency_factor:
        return (
            f"latency {cur_ms} ms > {latency_factor}x baseline {bl_ms} ms"
        )
    bl_len = len(baseline.get("response", "") or "")
    cur_len = len(current.get("response", "") or "")
    if bl_len:
        ratio = cur_len / bl_len
        low, high = 1 - length_band, 1 + length_band
        if not (low <= ratio <= high):
            return (
                f"length swing {ratio:.2f}x outside [{low:.2f}, {high:.2f}] "
                f"(baseline {bl_len} ch, current {cur_len} ch)"
            )
    return None
