"""Resource path resolution — container-aware.

Resolves model paths in this order:
  1. GLADOS_MODELS env var (e.g. /app/models)
  2. GLADOS_ROOT/models env var derived path
  3. Package root relative path (local dev fallback)
"""

import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_models_root() -> Path:
    """Return the absolute path to the models directory."""
    if models_dir := os.environ.get("GLADOS_MODELS"):
        return Path(models_dir)
    if root := os.environ.get("GLADOS_ROOT"):
        return Path(root) / "models"
    # Local dev fallback — walk up from this file to project root
    return Path(__file__).resolve().parents[3] / "models"


def resource_path(relative_path: str) -> Path:
    """Return absolute path to a model file.

    relative_path should be relative to the models directory,
    e.g. 'TTS/glados.onnx' or 'ASR/silero_vad.onnx'.

    Also accepts legacy paths like 'models/TTS/glados.onnx' —
    the leading 'models/' prefix is stripped automatically.
    """
    # Strip legacy 'models/' prefix if present
    if relative_path.startswith("models/") or relative_path.startswith("models\\"):
        relative_path = relative_path[7:]
    return get_models_root() / relative_path
