"""Pull the latest container image onto the Docker host, recreate
the service, and probe /health.

Credentials and host addressing MUST come from environment variables;
there are no defaults. This is a deployment helper, not a production
tool — never commit secrets or site-specific host addresses to source.

Required env:
  GLADOS_SSH_HOST        — Docker host address (e.g. 10.0.0.50)
  GLADOS_SSH_USER        — SSH user (default: root)
  GLADOS_SSH_PASSWORD    — SSH password
  GLADOS_COMPOSE_PATH    — absolute path to docker-compose.yml on the host
Optional env:
  GLADOS_IMAGE           — image ref (default: ghcr.io/<ORG>/glados-docker:latest)
  GLADOS_CONTAINER_NAME  — compose service + container name (default: glados)
"""
from __future__ import annotations

import os
import sys
import time

import paramiko


def _require(var: str) -> str:
    val = os.environ.get(var, "").strip()
    if not val:
        sys.stderr.write(
            f"Missing required env var {var!r}. See the module docstring.\n"
        )
        raise SystemExit(2)
    return val


HOST = _require("GLADOS_SSH_HOST")
USER = os.environ.get("GLADOS_SSH_USER", "root")
PASS = _require("GLADOS_SSH_PASSWORD")
CONTAINER = os.environ.get("GLADOS_CONTAINER_NAME", "glados")
IMAGE = os.environ.get("GLADOS_IMAGE", "ghcr.io/ORG/glados-docker:latest")
COMPOSE = _require("GLADOS_COMPOSE_PATH")


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return rc, out, err


def main() -> int:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=10)

    print(">>> current container:", flush=True)
    rc, out, _ = run(ssh, f"docker ps -a --filter name={CONTAINER} --format '{{{{.Names}}}} {{{{.Image}}}} {{{{.Status}}}}'")
    print(out.rstrip())

    # compose pull + up -d recreates the container onto the new image.
    # docker restart alone keeps the old image, so DO NOT use it here.
    print(f">>> compose pull {CONTAINER}", flush=True)
    rc, out, err = run(ssh, f"docker compose -f {COMPOSE} pull {CONTAINER}", timeout=600)
    print((out + err).rstrip())
    if rc != 0:
        return 1

    print(f">>> compose up -d --no-deps {CONTAINER}", flush=True)
    rc, out, err = run(ssh, f"docker compose -f {COMPOSE} up -d --no-deps {CONTAINER}", timeout=120)
    print((out + err).rstrip())
    if rc != 0:
        return 1

    print(">>> waiting for health...", flush=True)
    for attempt in range(60):
        time.sleep(2)
        rc, out, _ = run(ssh, f"docker inspect --format='{{{{.State.Health.Status}}}}' {CONTAINER}")
        status = out.strip()
        if status == "healthy":
            print(f"healthy after {attempt * 2}s")
            break
        if attempt % 5 == 0:
            print(f"  still {status!r} ({attempt * 2}s)", flush=True)
    else:
        print("did not reach healthy within 120s", file=sys.stderr)
        return 2

    print(">>> recent log tail:", flush=True)
    rc, out, _ = run(ssh, f"docker logs --tail 30 {CONTAINER} 2>&1")
    print(out.rstrip())

    ssh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
