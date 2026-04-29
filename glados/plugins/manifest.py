"""Pydantic models for the official MCP ``server.json`` manifest format.

Schema floor: ``2025-12-11`` — older schemas rejected with a clear
error. Source of truth:
https://raw.githubusercontent.com/modelcontextprotocol/registry/refs/heads/main/docs/reference/server-json/generic-server-json.md

These models validate strictly at the top level (extra fields
rejected) but allow arbitrary keys under ``_meta`` per spec
(reverse-DNS namespacing). GLaDOS-specific extensions live under
``com.synssins.glados/*`` keys — see
``docs/plugins-architecture.md``.

Companion type ``RuntimeConfig`` is GLaDOS's own per-plugin runtime
state (operator-resolved env values, enable/disable, package selection)
and lives alongside each plugin's ``server.json`` as ``runtime.yaml``.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


# ── server.json sub-models ────────────────────────────────────────────


class Repository(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: HttpUrl
    source: str
    subfolder: str | None = None
    id: str | None = None


class Transport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["stdio", "streamable-http", "sse"]
    url: str | None = None  # may include {placeholders}; not required for stdio


class Variable(BaseModel):
    """Common shape for argument / env / header value descriptors.

    Fields with this shape appear in three places: ``packageArguments``,
    ``runtimeArguments``, ``environmentVariables``, ``remotes[].headers``.
    Pydantic models below pick the relevant subset.
    """

    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    default: str | None = None
    is_required: bool = Field(default=False, alias="isRequired")
    is_secret: bool = Field(default=False, alias="isSecret")
    format: str | None = None
    choices: list[str] | None = None


class EnvironmentVariable(Variable):
    """A single ``environmentVariables[]`` entry."""

    name: str  # env var name, e.g. ``SONARR_API_KEY``


class RemoteHeader(Variable):
    """A single ``remotes[].headers[]`` entry."""

    name: str  # header name, e.g. ``Authorization``


class InputArgument(Variable):
    """An ``packageArguments[]`` / ``runtimeArguments[]`` entry."""

    type: Literal["positional", "named"]
    name: str | None = None  # required when type=="named"
    value: str | None = None
    value_hint: str | None = Field(default=None, alias="valueHint")
    is_repeated: bool = Field(default=False, alias="isRepeated")
    variables: dict[str, Variable] | None = None  # nested templating

    @field_validator("name")
    @classmethod
    def _named_requires_name(cls, v: str | None, info) -> str | None:
        if info.data.get("type") == "named" and not v:
            raise ValueError("InputArgument with type='named' requires 'name'")
        return v


class Package(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    registry_type: Literal["npm", "pypi", "nuget", "oci", "mcpb"] = Field(
        alias="registryType"
    )
    registry_base_url: str | None = Field(default=None, alias="registryBaseUrl")
    identifier: str
    version: str
    runtime_hint: Literal["npx", "uvx", "dnx"] | None = Field(
        default=None, alias="runtimeHint"
    )
    transport: Transport
    package_arguments: list[InputArgument] = Field(
        default_factory=list, alias="packageArguments"
    )
    runtime_arguments: list[InputArgument] = Field(
        default_factory=list, alias="runtimeArguments"
    )
    environment_variables: list[EnvironmentVariable] = Field(
        default_factory=list, alias="environmentVariables"
    )
    file_sha256: str | None = Field(default=None, alias="fileSha256")


class Remote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["streamable-http", "sse"]
    url: str
    variables: dict[str, Variable] | None = None
    headers: list[RemoteHeader] = Field(default_factory=list)


# ── server.json top-level ─────────────────────────────────────────────


class ServerJSON(BaseModel):
    """Top-level ``server.json`` document.

    Validates against the official MCP Registry schema. Strict at the
    top level (extra fields rejected) except for ``_meta`` which is
    open-ended per spec.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_url: str | None = Field(default=None, alias="$schema")
    name: str
    description: str
    title: str | None = None
    version: str
    website_url: str | None = Field(default=None, alias="websiteUrl")
    repository: Repository | None = None
    packages: list[Package] = Field(default_factory=list)
    remotes: list[Remote] = Field(default_factory=list)
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")

    @field_validator("packages")
    @classmethod
    def _at_least_one_install_method(
        cls, packages: list[Package], info
    ) -> list[Package]:
        # Validation: must have either packages[] or remotes[].
        # ``info.data["remotes"]`` may not be set yet when packages is
        # validated first, so we just check that this side has entries
        # OR we'll catch it at model_validator stage; for now, the
        # cross-check happens in load_plugin().
        return packages

    # ── GLaDOS-specific _meta accessors ───────────────────────────────

    @property
    def glados_category(self) -> str:
        return self._meta_str("com.synssins.glados/category", default="utility")

    @property
    def glados_icon(self) -> str:
        return self._meta_str("com.synssins.glados/icon", default="plug")

    @property
    def glados_min_version(self) -> str | None:
        return self._meta_str("com.synssins.glados/min_glados_version", default=None)

    @property
    def glados_persona_role(self) -> Literal["interactive", "autonomy", "both"]:
        v = self._meta_str(
            "com.synssins.glados/recommended_persona_role", default="both"
        )
        if v not in ("interactive", "autonomy", "both"):
            return "both"
        return v  # type: ignore[return-value]

    def _meta_str(self, key: str, default: str | None = None) -> str | None:
        if not self.meta:
            return default
        v = self.meta.get(key)
        return v if isinstance(v, str) else default


# ── runtime.yaml — operator-resolved values ───────────────────────────


class RuntimeConfig(BaseModel):
    """GLaDOS's own per-plugin runtime state, written alongside
    ``server.json`` as ``runtime.yaml``.

    Holds the operator's resolved env / header / argument values for
    non-secret fields. Secret fields (``isSecret: true`` in the
    manifest) live in ``secrets.env`` (mode 0600, KEY=VALUE per line),
    NOT here.

    ``package_index`` selects which ``packages[]`` entry from
    ``server.json`` is active (for plugins that ship multiple package
    options, e.g. npm + oci). ``remote_index`` does the same for
    ``remotes[]``. Exactly one MUST be set.
    """

    model_config = ConfigDict(extra="forbid")

    plugin: str  # must match ServerJSON.name
    server_json_version: str = "2025-12-11"
    enabled: bool = True
    package_index: int | None = None
    remote_index: int | None = None
    env_values: dict[str, str] = Field(default_factory=dict)
    header_values: dict[str, str] = Field(default_factory=dict)
    arg_values: dict[str, str] = Field(default_factory=dict)
