"""Plugin layer errors."""
from __future__ import annotations


class PluginError(RuntimeError):
    """Base class for plugin-layer errors."""


class ManifestError(PluginError):
    """Raised when a ``server.json`` or ``runtime.yaml`` fails parsing /
    validation. Message should include the plugin directory so operators
    can locate the offending file."""


class InstallError(PluginError):
    """Raised when a plugin's package installation fails (network,
    missing runtime, package not found, etc.)."""
