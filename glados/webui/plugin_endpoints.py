"""Helpers for the /api/plugins/* HTTP surface in tts_ui.py.

Kept in a separate module so tts_ui.py doesn't grow further. Handlers
in tts_ui.py call into this module; this module contains the URL
fetching / SSRF guard / secret-merge logic that's worth unit-testing
without spinning up the full HTTP server.
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
        r = httpx.get(url, timeout=FETCH_TIMEOUT_S, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise InstallError(f"manifest fetch failed: {exc}") from exc
    if r.status_code != 200:
        raise InstallError(f"manifest fetch returned HTTP {r.status_code}")
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
    """Used by GET /api/plugins list view."""
    m = plugin.manifest
    return {
        "slug": plugin.directory.name,
        "name": m.name,
        "title": m.title or m.name,
        "version": m.version,
        "description": m.description,
        "category": m.glados_category,
        "icon": m.glados_icon,
        "enabled": plugin.enabled,
        "is_remote": plugin.is_remote(),
    }


def serialize_plugin_detail(plugin: Plugin) -> dict:
    """Used by GET /api/plugins/<slug>. Secrets returned as '***'."""
    m = plugin.manifest
    secrets_masked = {k: SECRET_PLACEHOLDER for k in plugin.secrets}
    return {
        "slug": plugin.directory.name,
        "manifest": m.model_dump(by_alias=True, exclude_none=True, mode="json"),
        "runtime": plugin.runtime.model_dump(mode="json"),
        "secrets": secrets_masked,
        "is_remote": plugin.is_remote(),
    }
