"""
HUB75 LED panel display package for GLaDOS.

Drives a 64x64 HUB75 matrix via WLED-MM (DDP + JSON API).
All rendering happens on the AIBox; only a raw pixel stream
is sent to the panel over UDP.
"""

from .display import Hub75Display
from .info_renderer import InfoPanelData
from .state_machine import EyeState

__all__ = ["Hub75Display", "EyeState", "InfoPanelData"]
