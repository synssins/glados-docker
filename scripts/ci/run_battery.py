"""Invoke the external harness at ``C:\\src\\glados-test-battery``.

The harness itself isn't in this git repo — it lives in a scratch
dir on AIBox. The battery-nightly workflow can't import it directly
since the workflow's working directory is the checked-out repo, so
this helper injects the harness path and calls ``run()``.

Rotates any prior ``results.json`` / ``harness.log`` to a timestamped
name so a nightly run doesn't clobber the operator's on-disk history.
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--harness-dir", type=Path,
        default=Path(r"C:\src\glados-test-battery"),
    )
    ap.add_argument("--max-tests", type=int, default=50)
    ap.add_argument("--start-idx", type=int, default=0)
    args = ap.parse_args(argv)

    if not args.harness_dir.exists():
        print(f"::error::harness dir not found: {args.harness_dir}")
        return 2

    # Rotate prior artefacts
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in ("results.json", "harness.log"):
        p = args.harness_dir / name
        if p.exists():
            rotated = args.harness_dir / f"{p.stem}.prerun-{ts}{p.suffix}"
            shutil.move(str(p), str(rotated))
            print(f"rotated {name} -> {rotated.name}")

    # Inject harness dir onto sys.path and invoke its run()
    sys.path.insert(0, str(args.harness_dir))
    import os
    os.chdir(args.harness_dir)
    from harness import run  # noqa: E402  (sys.path mutation required first)

    max_arg = args.max_tests if args.max_tests and args.max_tests > 0 else None
    run(max_tests=max_arg, start_idx=args.start_idx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
