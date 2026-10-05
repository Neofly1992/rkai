"""Capability DTOs. Model preprocessing never belongs to CLI or scheduling."""

from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator

from .errors import RkaiError
from .manifest import Strict

MAX_TEXT_CHARACTERS = 32768
MAX_IMAGE_BYTES = 16 * 1024 * 1024


class Message(Strict):
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, Field(min_length=1, max_length=MAX_TEXT_CHARACTERS)]


class ChatRequest(Strict):
    messages: Annotated[list[Message], Field(min_length=1, max_length=128)]
    max_tokens: Annotated[int, Field(ge=1, le=4096)] = 256
    temperature: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] = 0.0


class ClassificationRequest(Strict):
    # Boundary copies the image to session-owned media before handing it to worker.
    image: str
    top_k: Annotated[int, Field(ge=1, le=100)] = 5


class EmbeddingRequest(Strict):
    texts: Annotated[list[str], Field(min_length=1, max_length=32)]

    @field_validator("texts")
    @classmethod
    def bounded(cls, texts):
        if any(not text or len(text) > MAX_TEXT_CHARACTERS for text in texts):
            raise ValueError("text length outside capability budget")
        return texts


class RankedLabel(Strict):
    index: Annotated[int, Field(ge=0)]
    label: str
    score: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class ClassificationResult(Strict):
    labels: Annotated[list[RankedLabel], Field(min_length=1, max_length=100)]


class EmbeddingResult(Strict):
    vectors: Annotated[list[list[float]], Field(min_length=1, max_length=32)]
    dimension: Annotated[int, Field(ge=1, le=65536)]

    @field_validator("vectors")
    @classmethod
    def finite(cls, vectors):
        import math

        if any(not math.isfinite(v) for row in vectors for v in row):
            raise ValueError("nonfinite embedding")
        return vectors


class ChatDelta(Strict):
    text: str


class ChatResult(Strict):
    text: str
    finish_reason: Literal["stop", "length", "unknown"]


REQUESTS = {
    "chat": ChatRequest,
    "classification": ClassificationRequest,
    "embedding": EmbeddingRequest,
}
RESULTS = {"chat": ChatResult, "classification": ClassificationResult, "embedding": EmbeddingResult}
FEATURES = {"chat": {"text"}, "classification": set(), "embedding": set()}


def validate_request(capability: str, data: dict):
    if capability not in REQUESTS:
        raise RkaiError("unsupported_capability", "Capability is not implemented", 3)
    try:
        return REQUESTS[capability].model_validate(data)
    except ValidationError as e:
        raise RkaiError("invalid_input", "Request does not satisfy capability contract", 2) from e


def validate_result(capability: str, data: dict, request):
    try:
        result = RESULTS[capability].model_validate(data)
        if capability == "embedding" and (
            len(result.vectors) != len(request.texts)
            or any(len(v) != result.dimension for v in result.vectors)
        ):
            raise ValueError
        if capability == "classification" and len(result.labels) > request.top_k:
            raise ValueError
        return result.model_dump()
    except (ValidationError, ValueError, KeyError) as e:
        raise RkaiError("invalid_output", "Backend result violates capability contract") from e
