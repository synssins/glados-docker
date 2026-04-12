"""Set the WebUI authentication password for GLaDOS.

Usage:
    python -m glados.tools.set_password
    python -m glados.tools.set_password --password <password>

Hashes the password with bcrypt and writes it to configs/global.yaml.
Also auto-generates a session_secret if one isn't set.
"""

from __future__ import annotations

import getpass
import os
import secrets
import sys
from pathlib import Path

import bcrypt
import yaml


def _config_path() -> Path:
    """Resolve configs/global.yaml — container-aware."""
    candidates = [
        # GLADOS_CONFIG_DIR env var (container primary)
        Path(os.environ.get("GLADOS_CONFIG_DIR", "")) / "global.yaml",
        # Container default
        Path("/app/configs/global.yaml"),
        # CWD fallback (local dev)
        Path("configs/global.yaml"),
        # Relative to this file (src/glados/tools/ → project root)
        Path(__file__).resolve().parent.parent.parent.parent / "configs" / "global.yaml",
    ]
    for p in candidates:
        if p.exists():
            return p
    return Path("/app/configs/global.yaml")  # container default even if missing


def set_password(password: str) -> None:
    """Hash password and write to global.yaml."""
    config_path = _config_path()
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    auth = config.setdefault("auth", {})
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    auth["password_hash"] = pw_hash
    auth.setdefault("enabled", True)
    auth["enabled"] = True

    if not auth.get("session_secret"):
        auth["session_secret"] = secrets.token_hex(32)
        print("  Generated new session_secret.")

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"  Password hash written to {config_path}")
    print("  Restart the GLaDOS container for changes to take effect.")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Set GLaDOS WebUI password")
    parser.add_argument("--password", type=str, help="Password (prompted if omitted)")
    args = parser.parse_args()

    password = args.password
    if not password:
        password = getpass.getpass("Enter new WebUI password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("ERROR: Passwords do not match.")
            sys.exit(1)

    if len(password) < 4:
        print("ERROR: Password must be at least 4 characters.")
        sys.exit(1)

    set_password(password)


if __name__ == "__main__":
    main()
