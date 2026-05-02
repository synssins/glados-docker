"""Helpers for the /api/plugins/* HTTP surface in tts_ui.py.

Kept in a separate module so tts_ui.py doesn't grow further. Handlers
in tts_ui.py call into this module; this module contains the URL
fetching / SSRF guard / secret-merge logic that's worth unit-testing
without spinning up the full HTTP server.

Known limitation (v1): the SSRF guard resolves once via _resolve_safe_host,
then httpx does an independent resolution for the GET. A DNS-rebinding
attacker controlling the target's DNS could return a public IP for the
guard check and a private IP for the fetch. Mitigation (pinning the
resolved IP into the URL with a Host header) is deferred to v2 — the
admin-only install flow + manual operator workflow lower the risk.
"""
from __future__ import annotations

import ipaddress
import json
import socket
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from glados.plugins import (
    Plugin,
    discover_plugins,
    install_from_zip,
    install_plugin,
    load_plugin,
    plugin_to_mcp_config,
    remove_plugin,
    set_enabled,
    slugify,
)
from glados.plugins.errors import InstallError, ManifestError
from glados.plugins.loader import default_plugins_dir
from glados.plugins.manifest import ServerJSON
from glados.plugins.store import (
    load_runtime,
    load_secrets,
    save_runtime,
    save_secrets,
)


MAX_MANIFEST_BYTES = 256 * 1024
FETCH_TIMEOUT_S = 5.0
SECRET_PLACEHOLDER = "***"


def _resolve_safe_host(host: str) -> bool:
    """True iff host resolves only to public addresses. Conservative —
    refuses on resolution failure so a transient DNS issue doesn't
    silently let a private address through."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        addr_str = info[4][0]
        try:
            addr = ipaddress.ip_address(addr_str)
        except ValueError:
            return False
        if (addr.is_loopback or addr.is_private or
                addr.is_link_local or addr.is_multicast or
                addr.is_reserved or addr.is_unspecified):
            return False
    return True


def fetch_manifest(url: str) -> ServerJSON:
    """Fetch a server.json from `url` with all the install-flow guards.
    Raises InstallError with a user-facing message on any failure."""
    if not url.lower().startswith("https://"):
        raise InstallError("URL must use https://")
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not parsed.hostname:
        raise InstallError("URL has no host")
    if not _resolve_safe_host(parsed.hostname):
        raise InstallError(
            "URL host resolves to a loopback / private / link-local "
            "address; refusing for SSRF safety"
        )
    try:
        r = httpx.get(url, timeout=FETCH_TIMEOUT_S, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise InstallError(f"manifest fetch failed: {exc}") from exc
    if 300 <= r.status_code < 400:
        raise InstallError(
            f"manifest URL returned redirect {r.status_code} (Location: "
            f"{r.headers.get('location', '<missing>')!r}); refusing to follow "
            "for SSRF safety — adjust the source URL"
        )
    if r.status_code != 200:
        raise InstallError(f"manifest fetch returned HTTP {r.status_code}")
    content_length = r.headers.get("content-length")
    if content_length and int(content_length) > MAX_MANIFEST_BYTES:
        raise InstallError(
            f"manifest too large per Content-Length ({content_length} bytes; "
            f"max {MAX_MANIFEST_BYTES})"
        )
    text = r.text
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise InstallError(
            f"manifest too large (>{MAX_MANIFEST_BYTES} bytes)"
        )
    try:
        raw = json.loads(text)
    except Exception as exc:
        raise InstallError(f"manifest is not valid JSON: {exc}") from exc
    try:
        return ServerJSON.model_validate(raw)
    except Exception as exc:
        msg = str(exc)[:1024]
        raise InstallError(f"manifest failed schema validation: {msg}") from exc


def list_installed_slugs(plugins_dir: Path) -> set[str]:
    if not plugins_dir.exists():
        return set()
    return {
        d.name for d in plugins_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    }


def install_from_url(url: str, slug_hint: str | None = None) -> dict:
    """Full install-by-URL flow. Returns {slug, manifest}."""
    manifest = fetch_manifest(url)
    plugins_dir = default_plugins_dir()
    existing = list_installed_slugs(plugins_dir)
    slug = slug_hint or slugify(manifest.name, existing)
    if slug in existing:
        raise InstallError(f"slug {slug!r} is already installed")
    # Stash the source URL in _meta so the WebUI About tab can offer
    # "Reinstall from source" later. Reverse-DNS namespace per spec.
    meta = dict(manifest.meta or {})
    meta["com.synssins.glados/source_url"] = url
    manifest = manifest.model_copy(update={"meta": meta})
    install_plugin(plugins_dir, slug, manifest)
    return {
        "slug": slug,
        "manifest": manifest.model_dump(by_alias=True, exclude_none=True, mode="json"),
    }


def merge_runtime_save(
    plugin_dir: Path,
    env_values: dict[str, str] | None = None,
    header_values: dict[str, str] | None = None,
    arg_values: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,
) -> None:
    """Save runtime + secrets while honoring the secret-placeholder
    convention: any secret whose value is exactly '***' is left
    untouched (use prior value)."""
    rt = load_runtime(plugin_dir)
    update: dict[str, Any] = {}
    if env_values is not None:
        update["env_values"] = env_values
    if header_values is not None:
        update["header_values"] = header_values
    if arg_values is not None:
        update["arg_values"] = arg_values
    if update:
        new_rt = rt.model_copy(update=update)
        save_runtime(plugin_dir, new_rt)

    if secrets is not None:
        existing_secrets = load_secrets(plugin_dir)
        merged: dict[str, str] = dict(existing_secrets)
        for k, v in secrets.items():
            if v == SECRET_PLACEHOLDER:
                continue
            merged[k] = v
        save_secrets(plugin_dir, merged)


def serialize_plugin_summary(plugin: Plugin) -> dict:
    """Used by GET /api/plugins list view. Reads from the v2 manifest
    so v1-on-disk and v2-native plugins serialize identically."""
    m = plugin.manifest_v2
    return {
        "slug": plugin.directory.name,
        "name": m.name,
        "title": m.name,
        "version": m.version,
        "description": m.description,
        "category": m.category,
        "icon": m.icon or "plug",
        "enabled": plugin.enabled,
        "is_remote": plugin.is_remote(),
    }


def serialize_plugin_detail(plugin: Plugin) -> dict:
    """Used by GET /api/plugins/<slug>. Secrets returned as '***'."""
    m = plugin.manifest_v2
    secrets_masked = {k: SECRET_PLACEHOLDER for k in plugin.secrets}
    return {
        "slug": plugin.directory.name,
        "manifest": m.model_dump(mode="json"),
        "runtime": plugin.runtime.model_dump(mode="json"),
        "secrets": secrets_masked,
        "is_remote": plugin.is_remote(),
    }


# ── Browse-catalog helpers (GET /api/plugins/browse) ──────────────────

INDEX_REQUIRED_KEYS = {"name", "title", "category", "server_json_url"}


def fetch_index(url: str) -> list[dict]:
    """Fetch a single index.json with the same SSRF + size guards as
    ``fetch_manifest``. Returns the validated entries (each tagged with
    ``source_index = url``). Raises InstallError on any failure.

    Mirrors fetch_manifest's defenses on purpose — index URLs are
    operator-controlled but still touched by an admin click, so the
    redirect-bypass / private-IP / oversized-payload threats apply
    identically.
    """
    if not url.lower().startswith("https://"):
        raise InstallError("index URL must use https://")
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not parsed.hostname:
        raise InstallError("index URL has no host")
    if not _resolve_safe_host(parsed.hostname):
        raise InstallError(
            "index host resolves to a loopback / private / link-local "
            "address; refusing for SSRF safety"
        )
    try:
        r = httpx.get(url, timeout=FETCH_TIMEOUT_S, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise InstallError(f"index fetch failed: {exc}") from exc
    if 300 <= r.status_code < 400:
        raise InstallError(
            f"index URL returned redirect {r.status_code} (Location: "
            f"{r.headers.get('location', '<missing>')!r}); refusing to follow "
            "for SSRF safety — adjust the source URL"
        )
    if r.status_code != 200:
        raise InstallError(f"index fetch returned HTTP {r.status_code}")
    content_length = r.headers.get("content-length")
    if content_length and int(content_length) > MAX_MANIFEST_BYTES:
        raise InstallError(
            f"index too large per Content-Length ({content_length} bytes; "
            f"max {MAX_MANIFEST_BYTES})"
        )
    text = r.text
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise InstallError(f"index too large (>{MAX_MANIFEST_BYTES} bytes)")
    try:
        raw = json.loads(text)
    except Exception as exc:
        raise InstallError(f"index is not valid JSON: {exc}") from exc

    plugins = raw.get("plugins") if isinstance(raw, dict) else None
    if not isinstance(plugins, list):
        raise InstallError("index missing 'plugins' array")

    out: list[dict] = []
    for entry in plugins:
        # Silently drop malformed entries inside an otherwise-valid index;
        # one bad row shouldn't hide an index's good ones from the catalog.
        if not isinstance(entry, dict):
            continue
        if not INDEX_REQUIRED_KEYS.issubset(entry.keys()):
            continue
        if not str(entry["server_json_url"]).lower().startswith("https://"):
            continue
        e = dict(entry)
        e["source_index"] = url
        out.append(e)
    return out


def merge_browse_catalog(index_urls: list[str]) -> dict:
    """Walk every index URL; return ``{entries: [...], errors: [...]}``.
    One failed index does NOT fail the whole call. Entries deduped by
    ``name`` (last-index-wins so operators can override an upstream
    entry by adding their own index after it)."""
    by_name: dict[str, dict] = {}
    errors: list[dict] = []
    for url in index_urls:
        try:
            for entry in fetch_index(url):
                by_name[entry["name"]] = entry
        except InstallError as exc:
            errors.append({"url": url, "error": str(exc)})
    return {"entries": list(by_name.values()), "errors": errors}
