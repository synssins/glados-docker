"""SourceContext — per-request context for the CommandResolver.

Replaces the flat `Origin` string that today rides through the container
on the `X-GLaDOS-Origin` header. Carries the full set of signals the
resolver needs to decide what to do with an utterance:

  - origin        which client sent it (webui_chat, api_chat, etc.)
  - channel       derived category: "voice" or "chat"
  - session_id    stable per-session key for short-term memory
  - area_id       HA area context, when available (voice forwarded by HA
                  sets this; chat usually leaves it None)
  - principal     optional caller identity (user, satellite device id)
  - timestamp     request receipt time

Every request that enters `POST /v1/chat/completions` builds one of these
from request headers or (for OpenAI clients that can't set custom
headers) from an `extra_body.glados_context` field in the JSON body.

The container has exactly one user-command entry point — the OpenAI
chat-completions endpoint. This context object is what lets a single
resolver serve voice, chat, Discord, and anything else without
branching on the ingress.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from glados.observability.audit import Origin


Channel = Literal["voice", "chat"]


# Which origins are "voice" — i.e., the utterance was spoken, so the
# caller's physical area (if any) matters to disambiguation.
_VOICE_ORIGINS: frozenset[str] = frozenset({
    Origin.VOICE_MIC,
})

# Everything else is treated as chat by default. Autonomy and MQTT are
# machine-originated but behave like chat from the resolver's perspective
# (no physical area, text-only).
_CHAT_ORIGINS: frozenset[str] = frozenset({
    Origin.WEBUI_CHAT,
    Origin.API_CHAT,
    Origin.TEXT_STDIN,
    Origin.AUTONOMY,
    Origin.DISCORD,
    Origin.MQTT_CMD,
    Origin.UNKNOWN,
})


def _derive_channel(origin: str) -> Channel:
    """Map an Origin string to the two-valued channel type.

    Unknown origins fall back to 'chat' — the safer default because it
    requires explicit area context in the utterance rather than assuming
    a caller location we don't have.
    """
    if origin in _VOICE_ORIGINS:
        return "voice"
    return "chat"


@dataclass(frozen=True)
class SourceContext:
    """Immutable per-request context passed to the CommandResolver.

    Frozen because it's built once per request and then read by multiple
    layers (resolver, memory store, learned-context lookup, audit). An
    immutable object avoids accidental mutation in one layer leaking to
    another.
    """

    origin: str
    """One of Origin.* values. Unknown origins get normalized to UNKNOWN."""

    channel: Channel
    """Derived from origin. 'voice' only when the utterance was spoken."""

    session_id: str
    """Stable per-session key. Used to scope short-term memory. For the
    WebUI this is typically a browser-session UUID; for HA-forwarded
    voice it's typically the satellite device id (stable across
    utterances from the same physical location)."""

    area_id: str | None = None
    """HA area_id, when available. Voice forwarded by HA sets this;
    chat leaves it None unless the client chose to include it."""

    principal: str | None = None
    """Optional caller identity — username, satellite device id,
    OAuth subject, etc. For audit and per-principal preference
    overrides in a future phase."""

    satellite_device_id: str | None = None
    """Optional HA device_registry id for the originating satellite.
    Lets the resolver look up the area_id even if the request didn't
    include one directly."""

    timestamp: float = field(default_factory=time.time)
    """Unix epoch seconds at request receipt. Uses time.time() by
    default; tests can pass a fixed value."""

    # ---- Constructors ------------------------------------------------

    @classmethod
    def from_headers(
        cls,
        headers: Mapping[str, str],
        *,
        default_origin: str = Origin.UNKNOWN,
        default_session_id: str | None = None,
    ) -> SourceContext:
        """Build a SourceContext from HTTP request headers.

        Reads these (case-insensitive on the wire; callers are expected
        to hand us a case-normalized Mapping, or use a Mapping that
        handles case insensitivity):

          X-GLaDOS-Origin
          X-GLaDOS-Session-Id
          X-GLaDOS-Area-Id
          X-GLaDOS-Principal
          X-GLaDOS-Satellite-Device-Id

        `default_origin` is used when the client doesn't set the origin
        header. `default_session_id` is used when the client doesn't
        set a session id; if None, a random UUID4 is generated so the
        resolver always has something to key memory on (at the cost of
        that request not benefiting from any short-term memory — first-
        turn semantics).
        """
        get = _header_getter(headers)
        origin = _normalize_origin(get("X-GLaDOS-Origin", default_origin))
        # Session id — generate a UUID if neither header nor default
        # gave us one. Strip whitespace-only headers so they behave the
        # same as absent ones.
        session_id = _opt_str(get("X-GLaDOS-Session-Id")) or default_session_id or uuid.uuid4().hex
        area_id = _opt_str(get("X-GLaDOS-Area-Id"))
        principal = _opt_str(get("X-GLaDOS-Principal"))
        satellite = _opt_str(get("X-GLaDOS-Satellite-Device-Id"))
        return cls(
            origin=origin,
            channel=_derive_channel(origin),
            session_id=session_id,
            area_id=area_id,
            principal=principal,
            satellite_device_id=satellite,
        )

    @classmethod
    def from_extra_body(
        cls,
        extra: Mapping[str, Any] | None,
        *,
        default_origin: str = Origin.UNKNOWN,
        default_session_id: str | None = None,
    ) -> SourceContext:
        """Build from an OpenAI `extra_body.glados_context` dict.

        Fallback ingress for OpenAI client libraries that won't let
        callers set custom headers. The dict keys mirror the header
        names, lowercased and snake-cased:

          {
            "origin": "webui_chat",
            "session_id": "…",
            "area_id": "living_room",
            "principal": "chris",
            "satellite_device_id": "esphome_liv_sat_01"
          }
        """
        data = dict(extra or {})
        origin = _normalize_origin(str(data.get("origin", default_origin)))
        session_id = str(data.get("session_id") or default_session_id or uuid.uuid4().hex)
        area_id = _opt_str(data.get("area_id"))
        principal = _opt_str(data.get("principal"))
        satellite = _opt_str(data.get("satellite_device_id"))
        return cls(
            origin=origin,
            channel=_derive_channel(origin),
            session_id=session_id,
            area_id=area_id,
            principal=principal,
            satellite_device_id=satellite,
        )

    # ---- Convenience -------------------------------------------------

    def with_area(self, area_id: str | None) -> SourceContext:
        """Return a copy with `area_id` replaced. Useful when the
        satellite_device_id was supplied but the area_id wasn't, and
        the resolver has just looked it up in HA's device_registry."""
        # Frozen dataclass: build a new one.
        return SourceContext(
            origin=self.origin,
            channel=self.channel,
            session_id=self.session_id,
            area_id=area_id,
            principal=self.principal,
            satellite_device_id=self.satellite_device_id,
            timestamp=self.timestamp,
        )

    def to_audit_fields(self) -> dict[str, Any]:
        """Subset of fields that belong on an AuditEvent.extra dict.
        origin is audited separately; the rest goes here."""
        out: dict[str, Any] = {
            "channel": self.channel,
            "session_id": self.session_id,
        }
        if self.area_id:
            out["area_id"] = self.area_id
        if self.satellite_device_id:
            out["satellite_device_id"] = self.satellite_device_id
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_origin(raw: str) -> str:
    """Coerce an origin string to a known Origin value; anything else
    becomes UNKNOWN so downstream code can rely on the enum."""
    value = (raw or "").strip().lower()
    if value in Origin.ALL:
        return value
    return Origin.UNKNOWN


def _opt_str(v: Any) -> str | None:
    """None-safe string coercion — empty strings become None so the
    resolver treats absent-but-present fields the same as truly missing
    ones."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _header_getter(headers: Mapping[str, str]):
    """Return a callable that does a case-insensitive header lookup.

    HTTP header names are case-insensitive, but Python dicts are not.
    This builds a single lowercased index once and closes over it, so
    the caller gets a tidy `get("X-Name", default)` API.
    """
    lowered = {str(k).lower(): v for k, v in headers.items()}

    def _get(name: str, default: str | None = None) -> str | None:
        return lowered.get(name.lower(), default)

    return _get
