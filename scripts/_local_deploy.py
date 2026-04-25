"""Local deploy fallback — build on the Docker host directly when GHA is
unavailable (e.g., GitHub LFS bandwidth quota exhausted).

Usage:
    python scripts/_local_deploy.py

Requires the same env vars as scripts/deploy_ghcr.py:
    GLADOS_SSH_HOST, GLADOS_SSH_USER, GLADOS_SSH_PASSWORD, GLADOS_COMPOSE_PATH

Reads the current worktree, tars it (excluding caches and git metadata),
SCPs to the host, runs docker build there, and recreates the container.
No GHCR push, no LFS pull required — uses the LFS files that are already
checked out in the worktree.

Underscored filename to mark this as a transient/operational helper, not a
permanent piece of the deploy pipeline. Promote to scripts/deploy_local.py
if it proves repeatedly useful.
"""
from __future__ import annotations

import os
import sys
import tarfile
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
COMPOSE = _require("GLADOS_COMPOSE_PATH")
CONTAINER = os.environ.get("GLADOS_CONTAINER_NAME", "glados")
IMAGE = os.environ.get("GLADOS_IMAGE", "ghcr.io/synssins/glados-docker:latest")

# Worktree root = parent of scripts/
WORKTREE = Path(__file__).resolve().parent.parent
TARBALL_LOCAL = WORKTREE / "scripts" / "_build_context.tar.gz"
TARBALL_REMOTE = "/tmp/glados-build-context.tar.gz"
BUILD_DIR_REMOTE = "/tmp/glados-build"

EXCLUDE_DIRS = {
    ".git",
    ".worktrees",
    "__pycache__",
    ".pytest_cache",
    ".superpowers",
    ".venv",
    "node_modules",
    ".tasks",
}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".tar.gz", ".log")


def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = tarinfo.name.replace("\\", "/").split("/")
    for ex in EXCLUDE_DIRS:
        if ex in parts:
            return None
    if any(tarinfo.name.endswith(s) for s in EXCLUDE_SUFFIXES):
        return None
    return tarinfo


def _run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 600, stream: bool = False) -> int:
    print(f"\n>>> {cmd}", flush=True)
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    if stream:
        for line in iter(stdout.readline, ""):
            print(line.rstrip(), flush=True)
        for line in iter(stderr.readline, ""):
            print(line.rstrip(), file=sys.stderr, flush=True)
    rc = stdout.channel.recv_exit_status()
    if not stream:
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if out:
            print(out.rstrip(), flush=True)
        if err:
            print(err.rstrip(), file=sys.stderr, flush=True)
    return rc


def main() -> int:
    print(f"Worktree: {WORKTREE}")
    print(f"Tarball:  {TARBALL_LOCAL}")
    print(f"Host:     {USER}@{HOST}")
    print(f"Image:    {IMAGE}")
    print(f"Compose:  {COMPOSE}")

    print("\n>>> Creating build-context tarball locally...")
    if TARBALL_LOCAL.exists():
        TARBALL_LOCAL.unlink()
    with tarfile.open(TARBALL_LOCAL, "w:gz") as tf:
        tf.add(WORKTREE, arcname=".", filter=_filter)
    size_mb = TARBALL_LOCAL.stat().st_size / 1024 / 1024
    print(f"    {size_mb:.1f} MB")

    print("\n>>> Connecting to host...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=15)

    print("\n>>> Uploading tarball via SFTP...")
    sftp = ssh.open_sftp()
    t0 = time.time()
    sftp.put(str(TARBALL_LOCAL), TARBALL_REMOTE)
    sftp.close()
    print(f"    uploaded in {time.time()-t0:.1f}s")

    if _run(ssh, f"rm -rf {BUILD_DIR_REMOTE} && mkdir -p {BUILD_DIR_REMOTE}") != 0:
        return 1
    if _run(ssh, f"tar -xzf {TARBALL_REMOTE} -C {BUILD_DIR_REMOTE}", timeout=120) != 0:
        return 1
    if _run(
        ssh,
        f"cd {BUILD_DIR_REMOTE} && docker build -t {IMAGE} .",
        timeout=2400,
        stream=True,
    ) != 0:
        return 1
    if _run(
        ssh,
        f"docker compose -f {COMPOSE} up -d --no-deps --force-recreate {CONTAINER}",
        timeout=180,
    ) != 0:
        return 1

    print("\n>>> waiting for health...")
    for _ in range(60):
        time.sleep(2)
        rc = _run(ssh, f"docker inspect --format='{{{{.State.Health.Status}}}}' {CONTAINER}")
        if rc != 0:
            continue
        # Note: _run already printed; we re-fetch for the loop below.
        _, out, _ = ssh.exec_command(f"docker inspect --format='{{{{.State.Health.Status}}}}' {CONTAINER}")
        status = out.read().decode().strip()
        if status == "healthy":
            print(">>> healthy")
            break
    else:
        print(">>> WARNING: never reached healthy", file=sys.stderr)

    _run(ssh, f"rm -rf {BUILD_DIR_REMOTE} {TARBALL_REMOTE}")
    ssh.close()

    if TARBALL_LOCAL.exists():
        TARBALL_LOCAL.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
