"""URL helpers shared by the engine, webui sync, and dispatch sites.

The standard contract: operators paste a bare ``http://host:port`` into
the LLM & Services WebUI URL field, the system stores the bare base, and
protocol-internal paths (``/v1/chat/completions``, ``/v1/models``,
``/v1/audio/transcriptions``) are appended at dispatch time.

Backends that don't expose the OpenAI surface on the standard ``/v1/``
prefix (notably **OpenVINO Model Server**, which exposes chat completions
on ``/v3/v1/chat/completions`` and rejects ``/v1/chat/completions`` with
"Invalid request URL") need an escape hatch: when the operator pastes a
URL whose path **already ends in a recognised chat-completion suffix**,
the helpers respect that path verbatim instead of normalising it back to
bare. Legacy Ollama-style ``/api/chat`` URLs still get the strip-and-
reappend behaviour because their path doesn't match the suffix list.

Helpers:

  - ``strip_url_path(u)`` returns ``scheme://netloc`` for a URL, dropping
    any path/query/fragment — UNLESS the path ends in a chat-completion
    suffix, in which case the path is preserved.
  - ``compose_endpoint(base_url, path)`` joins a base with a path,
    returning the input verbatim when the input's path already ends
    with the requested suffix (or any chat-completion suffix). Otherwise
    falls back to the bare-and-append behaviour.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


# The canonical OpenAI chat-completion path. Operationally equivalent
# to a bare ``scheme://host:port`` (every spec-compliant backend appends
# this same path internally), so we treat it as bare and strip it. Lots
# of legacy code and stored configs assume "bare" storage; preserving
# this exact path would break the equivalence and trigger spurious
# config-drift warnings in the engine reconciler.
_CANONICAL_CHAT_PATH = "/v1/chat/completions"


def _path_is_authoritative(path: str) -> bool:
    """Return True when ``path`` ends with ``/chat/completions`` AND is
    something other than the canonical ``/v1/chat/completions`` —
    i.e. the operator typed a non-spec prefix on purpose (OpenVINO Model
    Server's ``/v3/v1/chat/completions``, an OpenAI-proxy's
    ``/openai/v1/chat/completions``, etc.) and stripping the path would
    route the dispatch site to a non-existent endpoint.

    The canonical ``/v1/chat/completions`` is treated as equivalent to
    the bare base — every spec-compliant backend serves it, and the
    dispatch site re-appends it anyway, so storing it explicitly is
    redundant.
    """
    p = path.rstrip("/")
    if not p.endswith("/chat/completions"):
        return False
    return p != _CANONICAL_CHAT_PATH


def strip_url_path(u: str | None) -> str:
    """Return ``scheme://netloc`` for a URL, dropping any path/query/fragment.

    Empty or whitespace-only input returns an empty string. URLs without
    a scheme are returned as-is (operator must include scheme — we don't
    auto-add one).

    **Exception**: if the URL's path already ends with a chat-completion
    suffix (e.g. ``/v3/v1/chat/completions`` for OVMS, or just plain
    ``/v1/chat/completions``), the path is preserved. The operator typed
    a complete intentional endpoint and stripping it would route the
    dispatch site to a non-existent ``/v1/chat/completions`` on backends
    like OVMS that only expose the OpenAI surface on a non-spec prefix.
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
    if _path_is_authoritative(parsed.path):
        # Preserve operator's complete endpoint. Drop trailing slash
        # for canonical form.
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def compose_endpoint(base_url: str | None, path: str) -> str:
    """Join a base URL with a path, respecting authoritative endpoints.

    Three cases:

    1. ``base_url`` is empty/whitespace → return ``""`` so callers can
       short-circuit.
    2. ``base_url`` already carries an authoritative chat-completion path
       (``/v3/v1/chat/completions``, ``/v1/chat/completions``, etc.) →
       return it verbatim. The operator typed a complete endpoint;
       respect it. This is the OVMS / non-spec-backend escape hatch.
    3. Otherwise → strip any stale path (legacy ``/api/chat`` etc.) and
       append the requested ``path``.

    ``path`` should start with ``/``; a leading slash is added if missing.
    """
    s = (base_url or "").strip()
    if not s:
        return ""
    if not path.startswith("/"):
        path = "/" + path
    parsed = urlparse(s)
    if _path_is_authoritative(parsed.path):
        # Operator's full URL wins — respect it verbatim.
        return s.rstrip("/")
    base = strip_url_path(s)
    return base + path
