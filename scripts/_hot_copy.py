"""Fast iteration helper: copy worktree files into the running container
and restart, skipping the docker-build cycle.

Usage:
    python scripts/_hot_copy.py glados/core/api_wrapper.py glados/cameras/discovery.py
    python scripts/_hot_copy.py --restart-only
    python scripts/_hot_copy.py --files-from changed.txt

Why this script exists. ``_local_deploy.py`` is the durable path: tar the
worktree, scp to the docker host, run ``docker build``, force-recreate the
container. Wall-clock 3-5 minutes. For tight iteration loops (probe-fix-
probe) we don't want to rebuild the image every time.

This helper bypasses the build by ``docker cp``ing individual files into
the container's writable layer at ``/app/<path>``, then ``docker restart
glados``. Wall-clock 15-25 seconds.

**Hard discipline rules — read before using:**

1. Edit the source file in the worktree FIRST. Always. Hot-copying without
   a corresponding source edit causes prod and git to diverge.
2. Run ``pytest`` locally before hot-copying. There's no test gate inside
   the container; one syntax error and the engine fails to start.
3. The hot-copied files live in the writable layer ONLY. Any
   ``docker compose up -d --force-recreate glados`` (which is what
   ``_local_deploy.py`` ends with) will destroy them. So the discipline
   is: hot-copy → iterate → ``git commit`` → ``git push`` →
   ``_local_deploy.py`` exactly once at the end of the session.
4. Use only on paths under ``/app/glados/`` (and friends — anything baked
   into the image). Bind-mounted paths (``/app/configs``, ``/app/data``,
   ``/app/logs``, ``/app/audio_files``, ``/app/certs``) should be edited
   on the host directly, not via this script.

Env vars (same set as ``_local_deploy.py``):
    GLADOS_SSH_HOST, GLADOS_SSH_USER, GLADOS_SSH_PASSWORD
    GLADOS_CONTAINER_NAME (default: ``glados``)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import paramiko


def _require(var: str) -> str:
    val = os.environ.get(var, "").strip()
    if not val:
        sys.stderr.write(f"Missing env var {var!r}\n")
        raise SystemExit(2)
    return val


HOST = _require("GLADOS_SSH_HOST")
USER = os.environ.get("GLADOS_SSH_USER", "root")
PASS = _require("GLADOS_SSH_PASSWORD")
CONTAINER = os.environ.get("GLADOS_CONTAINER_NAME", "glados")

WORKTREE = Path(__file__).resolve().parent.parent

# Paths inside the image that are SAFE to hot-copy. Anything else is either
# bind-mounted (host-side edits already work) or not in the image at all
# (tests/, .github/, docs/).
_SAFE_PREFIXES = ("glados/", "scripts/", "Dockerfile", "requirements.txt")


def _open_ssh() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASS, timeout=10)
    return client


def _path_is_safe(rel: str) -> bool:
    return any(rel == p or rel.startswith(p) for p in _SAFE_PREFIXES)


def _run(client: paramiko.SSHClient, cmd: str, *, label: str | None = None) -> tuple[int, str, str]:
    if label:
        print(f">>> {label}")
    stdin, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    if out.strip():
        print(out)
    if err.strip():
        print(f"  [stderr] {err.rstrip()}")
    return rc, out, err


def _copy_one(client: paramiko.SSHClient, sftp: paramiko.SFTPClient, rel: str) -> None:
    """Copy worktree file rel -> container:/app/rel via host /tmp staging."""
    src = WORKTREE / rel
    if not src.is_file():
        raise FileNotFoundError(f"worktree path not found: {src}")
    if not _path_is_safe(rel):
        raise ValueError(
            f"refusing to hot-copy {rel!r}: not under one of {_SAFE_PREFIXES}"
        )

    # Stage on host /tmp so docker cp can pull from there
    host_tmp = f"/tmp/_hot_copy_{int(time.time()*1000)}_{src.name}"
    print(f"  upload  {rel}  ({src.stat().st_size:,} bytes)")
    sftp.put(str(src), host_tmp)
    try:
        rc, _, _ = _run(
            client,
            f"docker cp {host_tmp} {CONTAINER}:/app/{rel}",
            label=None,
        )
        if rc != 0:
            raise RuntimeError(f"docker cp failed for {rel} (exit {rc})")
        # Match ownership to the glados user inside the container so the
        # python process can read its own files even if the source-side
        # file had different perms.
        _run(
            client,
            f"docker exec {CONTAINER} chown glados:root /app/{rel}",
            label=None,
        )
        print(f"  ok      /app/{rel}")
    finally:
        _run(client, f"rm -f {host_tmp}", label=None)


def _restart_and_wait(client: paramiko.SSHClient, *, timeout_s: float = 90.0) -> None:
    print(f">>> docker restart {CONTAINER}")
    t0 = time.time()
    rc, _, _ = _run(client, f"docker restart {CONTAINER}", label=None)
    if rc != 0:
        raise RuntimeError(f"docker restart failed (exit {rc})")

    print(">>> waiting for healthy...")
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        rc, out, _ = _run(
            client,
            f"docker inspect --format='{{{{.State.Health.Status}}}}' {CONTAINER}",
            label=None,
        )
        status = out.strip()
        if status != last:
            print(f"  status: {status}  (t={time.time()-t0:.1f}s)")
            last = status
        if status == "healthy":
            print(f">>> healthy in {time.time()-t0:.1f}s")
            return
        if status == "unhealthy":
            raise RuntimeError("container reported unhealthy after restart")
        time.sleep(2)
    raise RuntimeError(f"container did not reach healthy within {timeout_s:.0f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "paths", nargs="*",
        help="worktree-relative file paths to hot-copy into the container",
    )
    parser.add_argument(
        "--files-from", type=Path, default=None,
        help="newline-delimited file containing paths (one per line)",
    )
    parser.add_argument(
        "--restart-only", action="store_true",
        help="skip copying; just docker restart and wait for healthy",
    )
    parser.add_argument(
        "--no-restart", action="store_true",
        help="copy files but skip docker restart (useful for batched edits)",
    )
    args = parser.parse_args()

    paths: list[str] = list(args.paths)
    if args.files_from:
        for line in args.files_from.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                paths.append(line)

    if not paths and not args.restart_only:
        parser.error("provide one or more paths, or use --restart-only")

    client = _open_ssh()
    try:
        if not args.restart_only:
            sftp = client.open_sftp()
            try:
                for rel in paths:
                    _copy_one(client, sftp, rel)
            finally:
                sftp.close()

        if not args.no_restart:
            _restart_and_wait(client)
        else:
            print(">>> --no-restart: container NOT restarted; new code not active yet")
    finally:
        client.close()

    print(">>> hot-copy complete")


if __name__ == "__main__":
    main()
