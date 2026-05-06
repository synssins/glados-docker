"""Vision module — VLM HTTP client only."""
from .client import describe_images, VisionClientError

__all__ = ["VisionClientError", "describe_images"]
