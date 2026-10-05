"""Registered model semantics, shared by CPU and RKNN tensor backends."""

from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from .errors import RkaiError
from .manifest import Strict

# At most 32 MiB per decoded Pillow surface (including RGB's internal four-byte pixels).
# Two surfaces plus tensor arithmetic are reserved separately by admission.
MAX_IMAGE_PIXELS = 8 * 1024 * 1024
IMAGE_SURFACE_BYTES_PER_PIXEL = 4
FLOAT32_BYTES = 4
RGB_CHANNELS = 3
IMAGE_TENSOR_WORK_BUFFERS = 3  # Resized surface, float cast and contiguous layout copy.


def image_workspace_bytes(config):
    return (
        2 * MAX_IMAGE_PIXELS * IMAGE_SURFACE_BYTES_PER_PIXEL
        + IMAGE_TENSOR_WORK_BUFFERS * config.width * config.height * RGB_CHANNELS * FLOAT32_BYTES
    )


def image_tensor(path, config):
    """One preprocessing authority for inference and RKNN calibration."""
    import numpy as np
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise RkaiError("input_too_large", "Decoded image exceeds 8 Mi pixels", 2)
            image.load()
            with image.convert("RGB") as rgb:
                with rgb.resize((config.width, config.height)) as resized:
                    array = np.asarray(resized, dtype=np.float32)
        # In-place arithmetic keeps the normalization workspace bounded.
        array /= config.scale
        array -= np.asarray(config.mean, dtype=np.float32)
        array /= np.asarray(config.std, dtype=np.float32)
        if config.layout == "nchw":
            array = array.transpose(2, 0, 1)
        return np.ascontiguousarray(array[None, ...])
    except Image.DecompressionBombError as error:
        raise RkaiError("input_too_large", "Decoded image exceeds pixel budget", 2) from error
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise RkaiError("invalid_input", "Image cannot be decoded", 2) from error


class AdapterConfig(Strict):
    component: str = "model"
    conversion_record: str | None = None
    probe: dict | None = None


class ImageConfig(AdapterConfig):
    width: Annotated[int, Field(ge=1, le=4096)] = 224
    height: Annotated[int, Field(ge=1, le=4096)] = 224
    layout: Literal["nchw", "nhwc"] = "nchw"
    mean: list[float] = [0.485, 0.456, 0.406]
    std: list[float] = [0.229, 0.224, 0.225]
    scale: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 255.0
    labels: str
    input_name: str
    output_index: Annotated[int, Field(ge=0, le=64)] = 0
    logits: bool = True

    @model_validator(mode="after")
    def channels(self):
        import math

        if (
            len(self.mean) != 3
            or len(self.std) != 3
            or any(not math.isfinite(v) for v in self.mean + self.std)
            or any(v <= 0 for v in self.std)
        ):
            raise ValueError("RGB normalization needs three finite channels")
        if not self.labels.startswith("file:"):
            raise ValueError("labels must reference a manifest asset")
        return self


class TextEmbeddingConfig(AdapterConfig):
    tokenizer: str
    max_length: Annotated[int, Field(ge=1, le=8192)] = 256
    pooling: Literal["mean", "cls"] = "mean"
    normalize: bool = True
    input_names: list[Literal["input_ids", "attention_mask", "token_type_ids"]] = [
        "input_ids",
        "attention_mask",
        "token_type_ids",
    ]
    output_index: Annotated[int, Field(ge=0, le=64)] = 0

    @model_validator(mode="after")
    def refs(self):
        if not self.tokenizer.startswith("file:") or len(set(self.input_names)) != len(
            self.input_names
        ):
            raise ValueError("invalid tokenizer/input names")
        return self


class ChatConfig(AdapterConfig):
    prompt_format: Literal["chatml", "plain"] = "chatml"


class PromptChatConfig(AdapterConfig):
    tokenizer: str
    role_prefixes: dict[Literal["system", "user", "assistant"], str]
    message_suffix: str
    assistant_prefix: str

    @model_validator(mode="after")
    def complete_roles(self):
        if set(self.role_prefixes) != {"system", "user", "assistant"}:
            raise ValueError("prompt template needs all roles")
        if not self.tokenizer.startswith("file:"):
            raise ValueError("tokenizer must reference a manifest asset")
        return self


CONFIGS = {
    "image-classification": ImageConfig,
    "sentence-embedding": TextEmbeddingConfig,
    "gguf-chat": ChatConfig,
    "prompt-chat": PromptChatConfig,
}
ADAPTER_CAPABILITIES = {
    "image-classification": {"classification"},
    "sentence-embedding": {"embedding"},
    "gguf-chat": {"chat"},
    "prompt-chat": {"chat"},
}


def configuration(adapter):
    if adapter.id not in CONFIGS:
        raise RkaiError("unsupported_adapter", "No registered adapter for this model", 3)
    try:
        return CONFIGS[adapter.id].model_validate(adapter.config)
    except ValidationError as e:
        raise RkaiError("invalid_adapter", "Adapter configuration is invalid", 2) from e


class ImageAdapter:
    def __init__(self, config, assets):
        self.config = config
        self.labels = assets(config.labels).read_text().splitlines()
        if not self.labels or any(not label for label in self.labels):
            raise RkaiError("invalid_adapter", "Label asset is empty or malformed", 2)

    def infer(self, request, backends, cancel):
        import numpy as np

        cfg = self.config
        array = image_tensor(request["image_path"], cfg)
        output = backends[cfg.component].infer({cfg.input_name: array})[cfg.output_index]
        values = np.asarray(output, dtype=np.float64).reshape(-1)
        if len(values) != len(self.labels) or not np.isfinite(values).all():
            raise RkaiError("invalid_output", "Classification output/labels differ")
        if cfg.logits:
            values = np.exp(values - values.max())
            values /= values.sum()
        if (values < 0).any() or (values > 1).any() or not np.isclose(values.sum(), 1, atol=1e-3):
            raise RkaiError("invalid_output", "Classification probabilities are invalid")
        indices = np.argsort(-values, kind="stable")[: request["top_k"]]
        yield (
            "result",
            {
                "labels": [
                    {"index": int(i), "label": self.labels[i], "score": float(values[i])}
                    for i in indices
                ]
            },
        )


class EmbeddingAdapter:
    def __init__(self, config, assets):
        from tokenizers import Tokenizer

        self.config = config
        self.tokenizer = Tokenizer.from_file(str(assets(config.tokenizer)))
        # Reject oversized inputs rather than silently truncating semantic content.
        self.tokenizer.no_truncation()
        self.tokenizer.enable_padding()

    def infer(self, request, backends, cancel):
        import numpy as np

        cfg = self.config
        encoded = self.tokenizer.encode_batch(request["texts"])
        if any(len(e.ids) > cfg.max_length for e in encoded):
            raise RkaiError("context_limit", "Text exceeds embedding context budget", 2)
        tensors = {
            "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encoded], dtype=np.int64),
        }
        output = backends[cfg.component].infer({n: tensors[n] for n in cfg.input_names})[
            cfg.output_index
        ]
        if output.ndim != 3 or output.shape[:2] != tensors["input_ids"].shape:
            raise RkaiError("invalid_output", "Expected per-token hidden states")
        if cfg.pooling == "cls":
            vectors = output[:, 0]
        else:
            mask = tensors["attention_mask"][..., None]
            vectors = (output * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
        if cfg.normalize:
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if (norms == 0).any():
                raise RkaiError("invalid_output", "Cannot normalize zero embedding")
            vectors = vectors / norms
        yield "result", {"vectors": vectors.tolist(), "dimension": int(vectors.shape[1])}


class ChatAdapter:
    def __init__(self, config, assets):
        self.config = config

    def infer(self, request, backends, cancel):
        backend = backends[self.config.component]
        if self.config.prompt_format == "chatml":
            prompt = (
                "".join(
                    "<|im_start|>" + m["role"] + "\n" + m["content"] + "<|im_end|>\n"
                    for m in request["messages"]
                )
                + "<|im_start|>assistant\n"
            )
        else:
            prompt = "\n".join(m["content"] for m in request["messages"])
        if backend.token_count(prompt) + request["max_tokens"] > backend.config.context_length:
            raise RkaiError("context_limit", "Prompt plus generation exceeds context budget", 2)
        yield from backend.generate(prompt, request, cancel)


class PromptChatAdapter:
    def __init__(self, config, assets):
        from tokenizers import Tokenizer

        self.config = config
        self.tokenizer = Tokenizer.from_file(str(assets(config.tokenizer)))
        self.tokenizer.no_truncation()

    def infer(self, request, backends, cancel):
        cfg = self.config
        prompt = (
            "".join(
                cfg.role_prefixes[m["role"]] + m["content"] + cfg.message_suffix
                for m in request["messages"]
            )
            + cfg.assistant_prefix
        )
        backend = backends[cfg.component]
        if (
            len(self.tokenizer.encode(prompt).ids) + request["max_tokens"]
            > backend.config.context_length
        ):
            raise RkaiError("context_limit", "Prompt plus generation exceeds context budget", 2)
        yield from backend.generate(prompt, request, cancel)


ADAPTERS = {
    "image-classification": ImageAdapter,
    "sentence-embedding": EmbeddingAdapter,
    "gguf-chat": ChatAdapter,
    "prompt-chat": PromptChatAdapter,
}


ADAPTER_BACKENDS = {
    "image-classification": {"onnxruntime", "rknn"},
    "sentence-embedding": {"onnxruntime", "rknn"},
    "gguf-chat": {"llama-cpp"},
    "prompt-chat": {"rkllm"},
}
