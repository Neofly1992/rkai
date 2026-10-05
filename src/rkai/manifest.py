"""Strict acquisition contract. Execution-specific configuration belongs to M2."""

import hashlib
import json
import platform
import re
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import RkaiError

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(min_length=1, max_length=256)]
COMMIT = re.compile(r"^[0-9a-f]{40}$")
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def relative_path(value: str) -> str:
    # Decode too: URL encoding must never bypass path confinement.
    decoded = unquote(value)
    if (
        any(ord(c) < 32 for c in decoded)
        or any(c in decoded for c in "\\?#:")
        or any(part in ("", ".", "..") for part in decoded.split("/"))
    ):
        raise ValueError("expected confined POSIX relative path")
    return value


def public_url(value: str) -> str:
    if any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("URL contains whitespace/control characters")
    url = urlsplit(value)
    _ = url.port  # Validate malformed port syntax before any network access.
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("expected public HTTPS URL without credentials/query/fragment")
    if any(part in (".", "..") for part in unquote(url.path).split("/")):
        raise ValueError("URL path traversal")
    return value.rstrip("/")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def integer_versions(cls, data):
        if isinstance(data, dict):
            for name in ("api", "version"):
                if name in data and type(data[name]) is not int:
                    raise ValueError("version must be an integer")
        return data


class Model(Strict):
    id: Name
    revision: Name
    description: str | None = None
    license: str | None = None

    @field_validator("id")
    @classmethod
    def namespace(cls, v):
        relative_path(v)
        if "/" not in v:
            raise ValueError("model id requires namespace")
        return v


class HubSource(Strict):
    kind: Literal["huggingface", "modelscope"]
    origin: str
    repository: str
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]

    _url = field_validator("origin")(public_url)
    _repo = field_validator("repository")(relative_path)


class HttpsSource(Strict):
    kind: Literal["https"]
    base_url: str

    _url = field_validator("base_url")(public_url)


class Location(Strict):
    source: Name
    path: str

    _path = field_validator("path")(relative_path)


class File(Strict):
    path: str
    size: Annotated[int, Field(ge=0)]
    sha256: Digest
    location: Location | None = None

    _path = field_validator("path")(relative_path)


class Implementation(Strict):
    id: Name
    api: Literal[1]


class Adapter(Implementation):
    config: dict[str, Any]


class Capability(Strict):
    id: Name
    version: Literal[1]
    features: list[Name] = Field(default_factory=list)


class Target(Strict):
    os: Literal["linux", "darwin", "windows"]
    arch: Literal["aarch64", "x86_64"]
    accelerator: Literal["rockchip-npu", "cpu"]
    soc: Literal["rk3588"] | None = None

    @model_validator(mode="after")
    def npu(self):
        if self.accelerator == "rockchip-npu" and (
            self.os != "linux" or self.arch != "aarch64" or self.soc != "rk3588"
        ):
            raise ValueError("RK3588 NPU target must be linux/aarch64/rk3588")
        return self


class Component(Strict):
    id: Name
    backend: Implementation
    compatibility: Name
    files: dict[Name, Name]
    config: dict[str, Any] = Field(default_factory=dict)


class Plan(Strict):
    id: Name
    target: Target
    capabilities: Annotated[list[Capability], Field(min_length=1)]
    default_capability: Name
    adapter: Adapter
    components: Annotated[list[Component], Field(min_length=1)]

    @model_validator(mode="after")
    def references(self):
        unique([c.id for c in self.capabilities], "capability")
        unique([c.id for c in self.components], "component")
        if self.default_capability not in [c.id for c in self.capabilities]:
            raise ValueError("default capability missing")
        for c in self.capabilities:
            unique(c.features, "feature")
        return self


def unique(values: list[str], label: str):
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {label}")


def config_refs(value: Any) -> set[str]:
    if isinstance(value, str) and value.startswith("file:"):
        return {value[5:]}
    if isinstance(value, dict):
        return set().union(*(config_refs(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(config_refs(v) for v in value))
    return set()


class Manifest(Strict):
    schema_version: Literal["rkai.model/v0"] = Field(alias="schema")
    model: Model
    sources: dict[Name, Annotated[HubSource | HttpsSource, Field(discriminator="kind")]]
    files: dict[Name, File]
    plans: Annotated[list[Plan], Field(min_length=1)]

    @model_validator(mode="after")
    def references(self):
        unique([p.id for p in self.plans], "plan")
        paths = [f.path for f in self.files.values()]
        unique(paths, "file path")
        for path in paths:
            if any(
                path == reserved or path.startswith(reserved + "/")
                for reserved in ("manifest.json", "manifest.json.tmp")
            ) or any(p.startswith(path + "/") for p in paths):
                raise ValueError("file/directory or reserved manifest path collision")
        for f in self.files.values():
            if f.location and f.location.source not in self.sources:
                raise ValueError("unknown source reference")
        for p in self.plans:
            if self.closure(p) - self.files.keys():
                raise ValueError("unknown file reference")
        return self

    @staticmethod
    def closure(plan: Plan) -> set[str]:
        refs = config_refs(plan.adapter.config)
        for c in plan.components:
            refs.update(c.files.values())
            refs.update(config_refs(c.config))
        return refs

    def select(self, plan_id: str | None, target: str | None) -> Plan:
        candidates = [
            p
            for p in self.plans
            if (not plan_id or p.id == plan_id) and (not target or p.target.soc == target)
        ]
        if plan_id is None and target is None:
            host_os = platform.system().lower()
            host_arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(
                platform.machine().lower(), platform.machine().lower()
            )
            try:
                rk3588 = b"rk3588" in Path("/proc/device-tree/compatible").read_bytes()
            except OSError:
                rk3588 = False
            candidates = [
                p
                for p in candidates
                if p.target.os == host_os
                and p.target.arch == host_arch
                and (p.target.accelerator == "cpu" or rk3588)
            ]
            npu = [p for p in candidates if p.target.accelerator == "rockchip-npu"]
            candidates = npu or candidates
        if not candidates:
            raise RkaiError(
                "unsupported_plan",
                "No matching acquisition plan; for another host use --target or --plan",
                3,
            )
        if len(candidates) != 1:
            raise RkaiError(
                "ambiguous_plan", "Choose --plan: " + ", ".join(p.id for p in candidates), 2
            )
        return candidates[0]


def no_duplicates(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("duplicate JSON key")
        result[k] = v
    return result


def reject_constant(value):
    raise ValueError("non-finite JSON number")


def parse(raw: bytes) -> Manifest:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise RkaiError("invalid_manifest", "Manifest exceeds 4 MiB", 2)
    try:
        data = json.loads(
            raw,
            object_pairs_hook=no_duplicates,
            parse_constant=reject_constant,
        )
        return Manifest.model_validate(data)
    except ValidationError as e:
        errors = e.errors(include_input=False, include_url=False)
        unsupported = any(
            item["type"] == "literal_error" and item["loc"][-1] in ("schema", "api", "version")
            for item in errors
        )
        # Report static reasons without echoing model input/configuration values.
        reasons = "; ".join(dict.fromkeys(item["msg"] for item in errors))[:512]
        raise RkaiError(
            "unsupported_contract" if unsupported else "invalid_manifest",
            "Invalid manifest: " + reasons,
            3 if unsupported else 2,
        ) from e
    except (ValueError, UnicodeError, RecursionError) as e:
        raise RkaiError(
            "invalid_manifest", "Invalid manifest JSON (duplicate keys/nonfinite/encoding)", 2
        ) from e


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
