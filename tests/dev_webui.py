"""Local dev launcher — starts ONLY the WebUI on a test port against real backends.

Used by `.claude/launch.json` (preview MCP) and by `tests/conftest.py`
(Playwright integration tests). Not invoked in production — the container
uses `python -m glados.server`, which wires up the full engine + API +
WebUI stack via /app/configs.

Points at real LAN services (AIBox = 192.168.1.75 for Ollama/speaches,
HA = 192.168.1.104) so the UI loads real entity data. Writes to
`tests/.tmp-glados-data/` so test runs do not clobber host state.

The WebUI needs an HA token to fetch entity state; we pick it up from
the gitignored `configs/config.yaml` if present, otherwise from the
HA_TOKEN env var. Falling back to an empty string still lets the
config pages render — only HA-dependent features degrade.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "tests" / ".tmp-glados-data"
for sub in (
    "audio/glados_ha",
    "audio/glados_archive",
    "audio/glados_tts_ui",
    "audio/chat_audio",
    "audio/glados_announcements",
    "audio/glados_commands",
    "data",
    "logs",
    "assets",
):
    (TMP / sub).mkdir(parents=True, exist_ok=True)

# Paths — isolated from production state
os.environ.setdefault("GLADOS_ROOT", str(TMP))
os.environ.setdefault("GLADOS_DATA", str(TMP / "data"))
os.environ.setdefault("GLADOS_LOGS", str(TMP / "logs"))
os.environ.setdefault("GLADOS_AUDIO", str(TMP / "audio"))
os.environ.setdefault("GLADOS_ASSETS", str(TMP / "assets"))

# Real LAN backends (AIBox + HA). Override via env before launch if needed.
os.environ.setdefault("OLLAMA_URL", "http://192.168.1.75:11434")
os.environ.setdefault("OLLAMA_AUTONOMY_URL", "http://192.168.1.75:11436")
os.environ.setdefault("OLLAMA_VISION_URL", "http://192.168.1.75:11435")
os.environ.setdefault("SPEACHES_URL", "http://192.168.1.75:5050")
os.environ.setdefault("VISION_URL", "http://192.168.1.75:8016")
os.environ.setdefault("HA_URL", "http://192.168.1.104:8123")
os.environ.setdefault("HA_WS_URL", "ws://192.168.1.104:8123/api/websocket")

# Pick up HA token from gitignored configs/config.yaml if the env doesn't
# already carry one. The YAML path is the operator's usual source of truth
# on this workstation, and env-overrides-YAML is the model validator's job.
if not os.environ.get("HA_TOKEN"):
    try:
        import yaml  # type: ignore
        cfg_path = ROOT / "configs" / "config.yaml"
        if cfg_path.exists():
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            tok = (data.get("home_assistant") or {}).get("token", "")
            if tok:
                os.environ["HA_TOKEN"] = tok
    except Exception:
        pass  # Non-fatal — the WebUI still loads without HA entity data

# Plain HTTP — no cert for dev
os.environ["SSL_ENABLED"] = "false"

# Make the repo importable
sys.path.insert(0, str(ROOT))

# Import AFTER env is set so config_store picks up our values.
from glados.webui.tts_ui import run_webui  # noqa: E402

PORT = int(os.environ.get("WEBUI_DEV_PORT", "28052"))
HOST = os.environ.get("WEBUI_DEV_HOST", "127.0.0.1")

if __name__ == "__main__":
    print(f"[dev_webui] Starting WebUI on http://{HOST}:{PORT}")
    print(f"[dev_webui] Tmp data dir: {TMP}")
    run_webui(host=HOST, port=PORT)
