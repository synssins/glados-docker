"""Regression: live-reload of auth config propagates without restart."""


def test_no_stale_auth_globals_in_tts_ui():
    """tts_ui.py used to cache _cfg.auth.* at import; those captures
    broke live-reload. See AUTH_DESIGN.md §2.7 / §7.3."""
    from glados.webui import tts_ui

    banned = {"_AUTH_ENABLED", "_AUTH_PASSWORD_HASH",
              "_AUTH_SESSION_SECRET", "_AUTH_SESSION_TIMEOUT_H"}
    present = banned & set(vars(tts_ui))
    assert not present, f"tts_ui still holds stale auth globals: {present}"
