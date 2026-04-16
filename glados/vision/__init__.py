"""Vision components — lightweight state/config only.

The container is pure middleware: local ONNX inference (FastVLM,
VisionProcessor) is NOT included.  Vision ML runs on the external
glados-vision service at VISION_URL; the container consumes results
via the camera_watcher autonomy agent.
"""

from .vision_config import VisionConfig
from .vision_request import VisionRequest
from .vision_state import VisionState

__all__ = ["VisionConfig", "VisionProcessor", "VisionRequest", "VisionState"]


def __getattr__(name: str):
    """Lazy stub for VisionProcessor — import succeeds but instantiation raises."""
    if name == "VisionProcessor":
        raise ImportError(
            "VisionProcessor is not available in the container build. "
            "Vision inference is handled by the external glados-vision service."
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
