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


class Config(Strict):
    data_dir: str = str(
        Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "rkai"
    )
    default_registry: Name = "hf"
    profile: Literal["release", "debug"] = "release"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] | None = None
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
