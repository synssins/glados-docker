"""Fail the battery-nightly workflow if the pass rate drops below a
tripwire threshold. Baseline 2026-04-20 was 55.9%; Phase 8.x
improvements should have lifted this. Tripwire at 45% so normal
fluctuations don't alarm but catastrophic regressions do.

Exit code 0 on OK-or-no-data, 1 on tripwire breach.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


DEFAULT_TRIPWIRE = 0.45


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--tripwire", type=float, default=DEFAULT_TRIPWIRE)
    args = ap.parse_args(argv)

    if not args.results.exists():
        print(f"::warning::results.json missing at {args.results}")
        return 0

    data = json.loads(args.results.read_text(encoding="utf-8"))
    rows = data.get("rows", [])
    if not rows:
        print("::warning::no rows in results.json")
        return 0

    passed = sum(1 for r in rows if r.get("pass_fail") in ("PASS", "QUERY_OK"))
    rate = passed / len(rows)
    print(f"pass rate: {rate:.2%} ({passed}/{len(rows)})")

    if rate < args.tripwire:
        print(
            f"::error::pass rate {rate:.1%} below "
            f"{args.tripwire:.0%} tripwire"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
