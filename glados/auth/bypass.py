"""Auth-bypass mode — populated in Task 9. This stub exists so Task 4
can call bypass.active() without an ImportError. See docs/AUTH_DESIGN.md §9.
"""
from __future__ import annotations


def active() -> bool:
    return False


def banner_html() -> str:
    return ""


def audit_tag(*, remote_addr: str = "") -> dict:
    return {}
