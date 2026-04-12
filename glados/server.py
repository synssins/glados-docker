"""GLaDOS container entrypoint.

Starts the API server (port 8015) and WebUI (port 8052) as threads,
then runs the GLaDOS engine on the main thread.

This replaces the two NSSM services (glados-api and glados-tts-ui)
that the host-native deployment uses.

Usage:
    python -m glados.server
    python -m glados.server --port 8015 --webui-port 8052
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from loguru import logger


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GLaDOS container server")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("GLADOS_PORT", "8015")),
        help="API listen port (default: 8015, env: GLADOS_PORT)",
    )
    parser.add_argument(
        "--webui-port",
        type=int,
        default=int(os.environ.get("WEBUI_PORT", "8052")),
        help="WebUI listen port (default: 8052, env: WEBUI_PORT)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--no-webui",
        action="store_true",
        help="Disable the WebUI admin panel",
    )
    parser.add_argument(
        "--input-mode",
        choices=["audio", "text", "both"],
        default=os.environ.get("GLADOS_INPUT_MODE", "text"),
        help="Input mode (default: text)",
    )
    return parser.parse_args()


def _start_webui(host: str, port: int) -> None:
    """Start the WebUI admin panel in a background thread."""
    try:
        from glados.webui.tts_ui import run_webui
        logger.info("Starting WebUI on {}:{}", host, port)
        run_webui(host=host, port=port)
    except ImportError:
        logger.warning("WebUI module not available — admin panel disabled")
    except Exception as exc:
        logger.error("WebUI failed to start: {}", exc)


def _ensure_dirs() -> None:
    """Create required runtime directories if they don't exist."""
    audio_base = Path(os.environ.get("GLADOS_AUDIO", "/app/audio_files"))
    data_dir = Path(os.environ.get("GLADOS_DATA", "/app/data"))
    logs_dir = Path(os.environ.get("GLADOS_LOGS", "/app/logs"))

    for d in [
        audio_base / "glados_ha",
        audio_base / "glados_archive",
        audio_base / "glados_announcements",
        audio_base / "glados_commands",
        audio_base / "chat_audio",
        audio_base / "chimes",
        data_dir,
        logs_dir,
    ]:
        d.mkdir(parents=True, exist_ok=True)


def main() -> None:
    args = _parse_args()

    logger.info("GLaDOS container starting")
    logger.info("  API port:   {}", args.port)
    logger.info("  WebUI port: {}", args.webui_port)
    logger.info("  Input mode: {}", args.input_mode)
    logger.info("  Config dir: {}", os.environ.get("GLADOS_CONFIG_DIR", "/app/configs"))

    # Ensure runtime directories exist
    _ensure_dirs()

    # Start WebUI in background thread
    if not args.no_webui:
        webui_thread = threading.Thread(
            target=_start_webui,
            args=(args.host, args.webui_port),
            name="WebUI",
            daemon=True,
        )
        webui_thread.start()

    # Start API wrapper — this imports the engine and runs it on the main thread
    # Import here so env vars are fully set before any glados module loads
    from glados.core.api_wrapper import main as api_main

    # Patch sys.argv so api_wrapper's argparse reads our values
    sys.argv = [
        "glados.server",
        "--port", str(args.port),
        "--host", args.host,
        "--input-mode", args.input_mode,
    ]

    logger.info("Handing off to API wrapper")
    api_main()


if __name__ == "__main__":
    main()
