"""GLaDOS plugin bundle manifest (``plugin.json``) -- v2 schema.

Replaces the v1 ``server.json``-only flow. A bundle is a zip containing
``plugin.json`` at the top level plus optional ``server.json`` (fallback),
README, icon, and bundled source.

See ``docs/superpowers/specs/2026-04-29-plugin-bundle-format-design.md``
for the canonical design.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class Setting(BaseModel):
    """One operator-configurable setting on the plugin."""
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: Literal["text", "url", "number", "boolean", "select", "secret"]
    required: bool = False
    description: str | None = None
    default: str | int | float | bool | None = None
    choices: list[str] | None = None

    @model_validator(mode="after")
    def _select_requires_choices(self) -> "Setting":
        if self.type == "select" and not self.choices:
            raise ValueError("select-type setting requires non-empty 'choices'")
        return self

    @property
    def is_required(self) -> bool:
        return self.required


class RegistryRuntime(BaseModel):
    """Spawn via uvx/npx fetching at runtime from PyPI/npm."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["registry"] = "registry"
    package: str  # form: "uvx:pkg@ver" or "npx:pkg@ver" or "dnx:pkg@ver"

    @field_validator("package")
    @classmethod
    def _well_formed_package(cls, v: str) -> str:
        if ":" not in v or "@" not in v.split(":", 1)[1]:
            raise ValueError(
                f"package must be of form '<runtime>:<name>@<version>', got {v!r}"
            )
        rt = v.split(":", 1)[0]
        if rt not in ("uvx", "npx", "dnx"):
            raise ValueError(f"unsupported runtime {rt!r}; want uvx/npx/dnx")
        return v


class BundledRuntime(BaseModel):
    """Spawn ``command + args`` from inside the unpacked bundle directory."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["bundled"] = "bundled"
    command: str
    args: list[str] = Field(default_factory=list)


class RemoteRuntime(BaseModel):
    """Connect via streamable-HTTP to an external endpoint."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["remote"] = "remote"
    url: HttpUrl
    headers: list[Setting] | None = None  # operator-resolved at install

    @field_validator("url")
    @classmethod
    def _https_only(cls, v: HttpUrl) -> HttpUrl:
        if v.scheme != "https":
            raise ValueError(f"remote URL must be https://, got {v}")
        return v


Runtime = RegistryRuntime | BundledRuntime | RemoteRuntime


class PluginJSON(BaseModel):
    """Top-level ``plugin.json`` document."""
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1)
    name: str
    description: str
    version: str
    category: str  # 'media' / 'home' / 'system' / 'dev' / 'utility' or anything
    icon: str | None = None
    persona_role: Literal["interactive", "autonomy", "both"] = "both"
    homepage: str | None = None
    runtime: Runtime = Field(discriminator="mode")
    settings: list[Setting] = Field(default_factory=list)
    # Lower-cased on validation so the chat-time matcher doesn't have
    # to re-normalize on every turn. Empty list = plugin opts out of
    # the keyword pre-filter (triage LLM still considers it).
    intent_keywords: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _v1_only(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"unsupported schema_version {v}; this build only handles v1")
        return v

    @field_validator("intent_keywords")
    @classmethod
    def _lower_keywords(cls, v: list[str]) -> list[str]:
        return [kw.strip().lower() for kw in v if kw and kw.strip()]


def v1_to_v2(server_json: dict, *, package_index: int | None, remote_index: int | None) -> PluginJSON:
    """Synthesize a PluginJSON view of a legacy v1 server.json so the WebUI
    form renderer and runner can treat all installs as v2.

    Exactly one of package_index / remote_index must be set (matches the
    Phase 2a runtime.yaml selector convention)."""
    name = server_json.get("name", "unknown")
    description = server_json.get("description", "")
    version = server_json.get("version", "0.0.0")
    meta = server_json.get("_meta") or {}
    category = meta.get("com.synssins.glados/category", "utility")
    icon = meta.get("com.synssins.glados/icon")
    persona_role = meta.get("com.synssins.glados/recommended_persona_role", "both")
    if persona_role not in ("interactive", "autonomy", "both"):
        persona_role = "both"

    if package_index is not None:
        pkg = server_json["packages"][package_index]
        rt_hint = pkg.get("runtimeHint")
        if not rt_hint:
            raise ValueError("v1 package missing runtimeHint")
        package = f"{rt_hint}:{pkg['identifier']}@{pkg['version']}"
        runtime: Runtime = RegistryRuntime(package=package)
        settings = [
            Setting(
                key=ev["name"],
                label=ev["name"],  # v1 has no operator-friendly label; use key
                type="secret" if ev.get("isSecret") else "text",
                required=ev.get("isRequired", False),
                description=ev.get("description"),
                default=ev.get("default"),
                choices=ev.get("choices"),
            )
            for ev in pkg.get("environmentVariables", [])
        ]
    elif remote_index is not None:
        rem = server_json["remotes"][remote_index]
        runtime = RemoteRuntime(url=rem["url"])
        settings = [
            Setting(
                key=h["name"],
                label=h["name"],
                type="secret" if h.get("isSecret") else "text",
                required=h.get("isRequired", False),
                description=h.get("description"),
                default=h.get("default"),
            )
            for h in rem.get("headers", [])
        ]
    else:
        raise ValueError("v1_to_v2 requires package_index or remote_index")

    return PluginJSON(
        schema_version=1,
        name=name,
        description=description or "(no description)",
        version=version,
        category=category,
        icon=icon,
        persona_role=persona_role,
        runtime=runtime,
        settings=settings,
    )
