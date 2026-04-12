"""
GIF-to-DDP streamer and WLED preset player for the HUB75 display.

Two-tier system:
  Tier 1 — WLED native presets (hand off to WLED effect engine).
  Tier 2 — GIF file decode → DDP frame stream (all processing on AIBox).

Pillow is required for Tier 2 (GIF decoding).
"""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

from .ddp import DdpSender
from .wled_client import WledClient

try:
    from PIL import Image
    _HAS_PILLOW = True
except ImportError:
    _HAS_PILLOW = False
    logger.warning("Pillow not installed — GIF file playback disabled (WLED presets still work)")


class GifPlayer:
    """Plays GIFs via DDP or triggers WLED native presets."""

    def __init__(
        self,
        sender: DdpSender,
        wled_client: WledClient,
        panel_w: int,
        panel_h: int,
    ) -> None:
        self._sender = sender
        self._wled = wled_client
        self._w = panel_w
        self._h = panel_h
        self._interrupted = False

    def interrupt(self) -> None:
        """Signal current playback to stop early."""
        self._interrupted = True

    # ── Tier 1: WLED native presets ───────────────────────────

    def play_preset(self, preset_id: int, duration_s: float = 5.0) -> None:
        """Hand off to a WLED native preset for a fixed duration.

        1. Let WLED effects take priority (lor=1).
        2. Activate the preset.
        3. Sleep for duration.
        4. Give DDP back priority (lor=0).
        """
        self._interrupted = False
        logger.debug("GIF: playing WLED preset {} for {:.1f}s", preset_id, duration_s)
        self._wled.set_live_override(realtime_priority=False)
        self._wled.set_preset(preset_id)

        # Interruptible sleep
        end = time.monotonic() + duration_s
        while time.monotonic() < end and not self._interrupted:
            time.sleep(0.1)

        self._wled.set_live_override(realtime_priority=True)
        logger.debug("GIF: preset {} done, DDP resumed", preset_id)

    # ── Tier 2: GIF file → DDP ───────────────────────────────

    def play_gif_file(self, path: str | Path, loops: int = 1) -> None:
        """Decode a GIF file and stream frames via DDP.

        All decoding and resizing happens on the AIBox; only the
        raw RGB pixel stream is sent over UDP.

        Args:
            path: Path to a .gif file.
            loops: Number of times to play (0 = loop until interrupted).

        Raises:
            FileNotFoundError: If the file does not exist.
            RuntimeError: If Pillow is not installed.
        """
        if not _HAS_PILLOW:
            logger.warning("GIF: Pillow not installed, cannot play {}", path)
            return

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"GIF file not found: {path}")

        self._interrupted = False
        logger.debug("GIF: streaming {} (loops={})", path.name, loops)

        try:
            img = Image.open(path)
            iteration = 0

            while not self._interrupted:
                iteration += 1
                if loops > 0 and iteration > loops:
                    break

                frame_idx = 0
                try:
                    while not self._interrupted:
                        img.seek(frame_idx)

                        # Resize and convert to RGB
                        frame = img.convert("RGB").resize(
                            (self._w, self._h), Image.LANCZOS
                        )
                        frame_bytes = frame.tobytes()
                        self._sender.send_frame(frame_bytes)

                        # Frame delay (GIF stores in ms, default 100ms)
                        delay_ms = img.info.get("duration", 100)
                        time.sleep(max(delay_ms / 1000.0, 0.01))

                        frame_idx += 1
                except EOFError:
                    # End of frames — loop or finish
                    pass

            img.close()
        except Exception as exc:
            logger.warning("GIF: error streaming {}: {}", path, exc)

    # ── Asset path helper ─────────────────────────────────────

    @staticmethod
    def get_asset_path(category: str, name: str, assets_dir: str) -> Path:
        """Resolve an asset path by category and name.

        Args:
            category: Subdirectory name (e.g. "weather", "reactions").
            name: File name (with or without .gif extension).
            assets_dir: Root assets directory.

        Returns:
            Resolved Path to the asset file.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        base = Path(assets_dir) / category
        # Try with and without .gif extension
        for candidate in [base / name, base / f"{name}.gif"]:
            if candidate.exists():
                return candidate
        raise FileNotFoundError(
            f"GIF asset not found: {category}/{name} in {assets_dir}"
        )
