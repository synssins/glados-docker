"""One-shot helper to insert (or revert) the VERBATIM RULE inside the
running container's personality_preprompt block.

Usage:
    python scripts/_inject_verbatim_rule.py apply
    python scripts/_inject_verbatim_rule.py revert

Reads `/srv/.../docker/appdata/glados/configs/glados_config.yaml` on the
Docker host, modifies the `personality_preprompt: - system: >-` block,
writes it back, and restarts the container so the engine reloads.

Operator-directed 2026-04-25 for a single TTS test. Underscored
filename — transient/operational helper, not part of the deploy
pipeline.
"""
from __future__ import annotations

import os
import sys
import time

import paramiko


def _require(var: str) -> str:
    val = os.environ.get(var, "").strip()
    if not val:
        sys.stderr.write(f"Missing env var {var!r}\n")
        raise SystemExit(2)
    return val


HOST = _require("GLADOS_SSH_HOST")
USER = os.environ.get("GLADOS_SSH_USER", "root")
PASS = os.environ.get("GLADOS_SSH_PASSWORD", "")
COMPOSE = os.environ.get(
    "GLADOS_COMPOSE_PATH",
    "/srv/dev-disk-by-uuid-8db26308-e3bf-41bc-8a5f-a3eb2c527f41/data/docker/compose/docker-compose.yml",
)
HOST_CONFIG_PATH = (
    "/srv/dev-disk-by-uuid-8db26308-e3bf-41bc-8a5f-a3eb2c527f41/data/docker/"
    "appdata/glados/configs/glados_config.yaml"
)

MARKER_BEGIN = "        # --- VERBATIM RULE BEGIN (transient, operator-directed) ---"
MARKER_END = "        # --- VERBATIM RULE END ---"

RULE_LINES = [
    "",
    MARKER_BEGIN,
    "        CRITICAL RULE — VERBATIM REPETITION:",
    '        When the user asks you to "repeat", "recite", "read", or "say" specific text',
    "        (especially text in quotes), output ONLY that text. Add nothing before. Add",
    "        nothing after. No commentary, no observation, no closing quip, no Aperture",
    "        Science aside, no rhetorical flourish. The user is using you as a precise TTS",
    "        voice for that phrase, and any addition defeats their purpose. Adding anything",
    "        is a critical persona failure.",
    MARKER_END,
]


def _connect() -> paramiko.SSHClient:
    if not PASS:
        sys.stderr.write("GLADOS_SSH_PASSWORD env var required\n")
        raise SystemExit(2)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=15)
    return ssh


def _read_file(ssh: paramiko.SSHClient, path: str) -> str:
    sftp = ssh.open_sftp()
    with sftp.open(path, "r") as f:
        data = f.read().decode("utf-8")
    sftp.close()
    return data


def _write_file(ssh: paramiko.SSHClient, path: str, content: str) -> None:
    sftp = ssh.open_sftp()
    with sftp.open(path, "w") as f:
        f.write(content.encode("utf-8"))
    sftp.close()


def _restart(ssh: paramiko.SSHClient) -> int:
    print(">>> docker compose restart glados ...")
    _, out, err = ssh.exec_command(
        f"docker compose -f {COMPOSE} restart glados", timeout=120
    )
    rc = out.channel.recv_exit_status()
    print(out.read().decode("utf-8", errors="replace").rstrip())
    e = err.read().decode("utf-8", errors="replace")
    if e:
        print(e.rstrip(), file=sys.stderr)
    return rc


def _wait_healthy(ssh: paramiko.SSHClient, timeout_s: int = 60) -> bool:
    print(">>> waiting for healthy...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(2)
        _, out, _ = ssh.exec_command(
            "docker inspect --format='{{.State.Health.Status}}' glados"
        )
        if out.read().decode().strip() == "healthy":
            print(">>> healthy")
            return True
    print(">>> WARNING: did not reach healthy in time", file=sys.stderr)
    return False


def apply_rule(content: str) -> str:
    if MARKER_BEGIN in content:
        print(">>> rule already present, no change")
        return content
    lines = content.split("\n")
    out: list[str] = []
    inserted = False
    in_block = False
    block_indent: int | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if not inserted and not in_block and stripped.startswith("- system: >-") and indent == 4:
            in_block = True
            block_indent = 8
            out.append(line)
            i += 1
            continue
        if in_block:
            if line.strip() == "":
                out.append(line)
                i += 1
                continue
            assert block_indent is not None
            if indent < block_indent:
                # block ended; insert before this line
                for r in RULE_LINES:
                    out.append(r)
                inserted = True
                in_block = False
                out.append(line)
                i += 1
                continue
        out.append(line)
        i += 1
    if in_block and not inserted:
        # File ended while still inside block
        for r in RULE_LINES:
            out.append(r)
        inserted = True
    if not inserted:
        raise RuntimeError("Could not locate personality_preprompt block")
    return "\n".join(out)


def revert_rule(content: str) -> str:
    if MARKER_BEGIN not in content:
        print(">>> rule already absent, no change")
        return content
    lines = content.split("\n")
    out: list[str] = []
    skip = False
    last_was_blank_before_marker = False
    for line in lines:
        if line == MARKER_BEGIN:
            # Drop the preceding blank line that we inserted, if it's the most recent line
            if out and out[-1].strip() == "":
                out.pop()
            skip = True
            continue
        if line == MARKER_END:
            skip = False
            continue
        if skip:
            continue
        out.append(line)
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("apply", "revert"):
        sys.stderr.write("usage: python scripts/_inject_verbatim_rule.py {apply|revert}\n")
        return 2

    action = sys.argv[1]
    ssh = _connect()
    try:
        original = _read_file(ssh, HOST_CONFIG_PATH)
        if action == "apply":
            modified = apply_rule(original)
        else:
            modified = revert_rule(original)
        if modified == original:
            return 0
        # Backup
        backup_path = HOST_CONFIG_PATH + ".bak.verbatim"
        _write_file(ssh, backup_path, original)
        print(f">>> backup at {backup_path}")
        _write_file(ssh, HOST_CONFIG_PATH, modified)
        print(f">>> wrote {HOST_CONFIG_PATH}")
        # Restart so engine reloads
        if _restart(ssh) != 0:
            print(">>> restart failed", file=sys.stderr)
            return 1
        _wait_healthy(ssh)
    finally:
        ssh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
