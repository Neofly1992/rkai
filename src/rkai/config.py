import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from .errors import RkaiError
from .manifest import Name, Strict, public_url


class Registry(Strict):
    kind: Literal["huggingface", "modelscope", "https"]
    origin: str
    mirrors: list[str] = Field(default_factory=list)

    _origin = field_validator("origin")(public_url)

    @field_validator("mirrors")
    @classmethod
    def urls(cls, values):
        return [public_url(v) for v in values]


MIB = 1024 * 1024


class HttpLimits(Strict):
    json_bytes: int = Field(default=16 * MIB, ge=1, le=1024 * MIB)
    multipart_bytes: int = Field(default=64 * MIB, ge=1, le=4096 * MIB)
    media_file_bytes: int = Field(default=32 * MIB, ge=1, le=1024 * MIB)
    metadata_bytes: int = Field(default=MIB, ge=1, le=16 * MIB)
    media_count: int = Field(default=8, ge=1, le=128)


class HttpConfig(Strict):
    host: Literal["127.0.0.1", "::1"] = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    preload: str | None = Field(default=None, min_length=1, max_length=256)
    queue_capacity: int = Field(default=8, ge=0, le=128)
    # Includes transport/parsed inputs and output buffers. Reserved at model admission.
    memory_bytes: int = Field(default=128 * MIB, ge=MIB, le=4096 * MIB)
    staging_bytes: int = Field(default=256 * MIB, ge=MIB, le=16384 * MIB)
    request_seconds: float = Field(default=300.0, ge=0.1, le=3600, allow_inf_nan=False)
    drain_seconds: float = Field(default=10.0, ge=0.1, le=60, allow_inf_nan=False)
    limits: HttpLimits = Field(default_factory=HttpLimits)


class Config(Strict):
    data_dir: str = str(
        Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "rkai"
    )
    default_registry: Name = "hf"
    profile: Literal["release", "debug"] = "release"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] | None = None
    http: HttpConfig = Field(default_factory=HttpConfig)
    allow_cpu_fallback: bool = False
    memory_budget_mb: int = Field(default=2048, ge=128, le=262144)
    memory_reserve_mb: int = Field(default=256, ge=0, le=262144)
    runtime_paths: dict[Literal["rknn", "rkllm"], str] = Field(default_factory=dict)
    registries: dict[Name, Registry] = Field(
        default_factory=lambda: {
            "hf": Registry(kind="huggingface", origin="https://huggingface.co"),
            "ms": Registry(kind="modelscope", origin="https://modelscope.cn"),
        }
    )

    @model_validator(mode="after")
    def paths(self):
        if not Path(self.data_dir).expanduser().is_absolute():
            raise ValueError("data_dir must be absolute")
        if self.default_registry not in self.registries:
            raise ValueError("default registry not configured")
        for v in self.runtime_paths.values():
            if not Path(v).is_absolute():
                raise ValueError("runtime path must be absolute")
        return self


def load(path: str | None = None, data_dir: str | None = None) -> Config:
    location = (
        Path(path).expanduser()
        if path
        else Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "rkai/config.toml"
    )
    try:
        data = tomllib.loads(location.read_text()) if location.exists() else {}
        if path and not location.exists():
            raise RkaiError("missing_config", "Specified config does not exist", 2)
        if data_dir:
            data["data_dir"] = data_dir
        if not isinstance(data.get("registries", {}), dict):
            raise ValueError("registries must be a TOML table")
        # Built-in sources remain available when adding custom registries.
        data["registries"] = {
            **{k: v.model_dump() for k, v in Config().registries.items()},
            **data.get("registries", {}),
        }
        return Config.model_validate(data)
    except (OSError, ValueError, ValidationError) as e:
        raise RkaiError("invalid_config", "Invalid or unreadable configuration", 2) from e
