"""URL helpers shared by the engine, webui sync, and dispatch sites.

Operators paste a bare ``http://host:port`` into the LLM & Services WebUI
URL field. The system stores the bare base; protocol-internal paths
(``/v1/chat/completions``, ``/v1/models``, ``/v1/audio/transcriptions``)
are appended only at dispatch time so the user never has to type or
know about them.

Two helpers live here so both the save-side normalizer and the dispatch
sites can share one definition:

  - ``strip_url_path(u)`` returns the bare ``scheme://host:port`` form,
    tolerantly stripping any path the operator might have pasted (legacy
    ``/api/chat``, ``/v1/chat/completions``, ``/api/tags``, ``/v1/models``,
    or anything else past ``host:port``). Empty/whitespace input → empty.
  - ``compose_endpoint(base_url, path)`` joins a bare base with a path,
    handling the case where someone passed in a base that still has a
    partial path. Always strips the base first.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def strip_url_path(u: str | None) -> str:
    """Return ``scheme://netloc`` for a URL, dropping any path/query/fragment.

    Empty or whitespace-only input returns an empty string. URLs without
    a scheme are returned as-is (operator must include scheme — we don't
    auto-add one).
    """
    s = (u or "").strip()
    if not s:
        return ""
    parsed = urlparse(s)
    if not parsed.scheme or not parsed.netloc:
        # Malformed (no scheme, no host) — return original stripped input
        # so callers with their own validation can flag it. The save-side
        # validator rejects these before they ever reach storage.
        return s.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def compose_endpoint(base_url: str | None, path: str) -> str:
    """Join a bare base URL with a relative path.

    ``base_url`` may be bare (``http://host:port``) or carry a stale path
    (``http://host:port/v1/chat/completions``); either form is normalized
    to the bare base before appending. ``path`` should start with ``/``.

    Returns an empty string if ``base_url`` is empty / whitespace-only,
    so callers can short-circuit cleanly.
    """
    base = strip_url_path(base_url)
    if not base:
        return ""
    if not path.startswith("/"):
        path = "/" + path
    return base + path
