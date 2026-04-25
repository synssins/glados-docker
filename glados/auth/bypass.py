"""Auth-bypass mode — compose-only GLADOS_AUTH_BYPASS=1 env flag.

Disables all auth checks for the container's run. Banner must be
visible on every HTML page; audit events carry operator_id="bypass:<ip>"
and auth_bypass=True. See docs/AUTH_DESIGN.md §9.

The flag is read ONCE at module import. There is no UI toggle, no
config-file entry, no API endpoint — compose only.
"""
from __future__ import annotations

import os
import threading
import time

from loguru import logger


_active = os.environ.get("GLADOS_AUTH_BYPASS", "").strip().lower() in {
    "1", "true", "yes", "on",
}


_BANNER_HTML = """
<div id="glados-auth-bypass-banner" style="
    position: sticky; top: 0; z-index: 9999;
    background: #c81010; color: #ffffff;
    padding: 12px 16px; font-weight: 700;
    font-family: system-ui, sans-serif;
    text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.5);">
  &#9888; AUTHENTICATION BYPASS MODE - anyone with network access to
  this WebUI has full admin control. Remove
  <code>GLADOS_AUTH_BYPASS</code> from docker-compose.yml and restart
  the container to resume normal authentication.
</div>
"""


def active() -> bool:
    """True iff GLADOS_AUTH_BYPASS is set to a truthy value."""
    return _active


def banner_html() -> str:
    """Banner HTML to inject into every page when bypass is active.
    Empty string when inactive."""
    return _BANNER_HTML if _active else ""


def audit_tag(*, remote_addr: str = "") -> dict:
    """Returns audit-event extra fields when bypass is active.

    Empty dict when inactive (so callers can do `audit_event(**bypass.audit_tag())`
    safely).
    """
    if not _active:
        return {}
    return {
        "auth_bypass": True,
        "operator_id": f"bypass:{remote_addr or 'unknown'}",
    }


def _periodic_warning():
    """Background thread that emits a WARN every 15 minutes while
    bypass is active. Daemon thread — exits when the main thread exits."""
    while _active:
        time.sleep(15 * 60)
        if _active:  # re-check in case of test cleanup
            logger.warning(
                "GLaDOS is running in AUTH BYPASS MODE. "
                "Remove GLADOS_AUTH_BYPASS from compose and restart."
            )


if _active:
    logger.error(
        "\n" + "=" * 72 + "\n"
        "  AUTHENTICATION BYPASS MODE ACTIVE\n"
        "  All auth checks are disabled for this container run.\n"
        "  Remove GLADOS_AUTH_BYPASS from docker-compose.yml to restore.\n"
        + "=" * 72
    )
    threading.Thread(target=_periodic_warning, daemon=True).start()
