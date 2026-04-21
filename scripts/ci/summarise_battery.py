"""Summarise `results.json` for the battery-nightly workflow.

Emits a human-readable tally to stdout AND appends a markdown table
to `$GITHUB_STEP_SUMMARY` so the run's summary page on GitHub shows
pass/fail counts inline. Non-fatal if the file is missing (e.g. the
run crashed before first write) — surfaces a warning instead.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    args = ap.parse_args(argv)

    if not args.results.exists():
        print(f"::warning::results.json missing at {args.results}")
        return 0

    try:
        data = json.loads(args.results.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"::error::results.json malformed: {exc}")
        return 1

    rows = data.get("rows", [])
    by: dict[str, int] = {}
    for r in rows:
        key = r.get("pass_fail", "?")
        by[key] = by.get(key, 0) + 1
    total = len(rows)
    print(f"Total: {total}")
    for k, v in sorted(by.items()):
        pct = (v / total * 100) if total else 0
        print(f"  {k}: {v} ({pct:.1f}%)")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("## Battery Results\n\n")
            f.write("| Disposition | Count | % |\n|---|---:|---:|\n")
            for k, v in sorted(by.items()):
                pct = (v / total * 100) if total else 0
                f.write(f"| {k} | {v} | {pct:.1f}% |\n")
            f.write(f"\n**Total:** {total} tests\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
