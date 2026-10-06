"""Explicit protocol subsets; capability validation remains authoritative."""

import json
import time
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from .capabilities import Message, validate_request
from .errors import RkaiError
from .manifest import Name, Strict


class Chat(Strict):
    model: Name
    messages: list[Message]
    max_tokens: int = 256
    max_completion_tokens: int | None = None
    temperature: float = 0.0
    stream: bool = False
    n: Annotated[int, Field(ge=1, le=1)] = 1


class Embeddings(Strict):
    model: Name
    input: str | list[str]
    encoding_format: Literal["float"] = "float"


class TextBlock(Strict):
    type: Literal["text"]
    text: str


class AnthropicMessage(Strict):
    role: Literal["user", "assistant"]
    content: str | list[TextBlock]


class Messages(Strict):
    model: Name
    messages: Annotated[list[AnthropicMessage], Field(min_length=1, max_length=128)]
    max_tokens: int
    system: str | list[TextBlock] | None = None
    temperature: float = 0.0
    stream: bool = False


class Native(Strict):
    model: Name
    capability: Name
    input: dict
    parameters: dict = Field(default_factory=dict)
    stream: bool = False


def text(value):
    if isinstance(value, str):
        return value
    return "".join(block.text for block in value)


def parse_request(protocol, data, media=None):
    schemas = {"openai": Chat, "embeddings": Embeddings, "anthropic": Messages, "native": Native}
    try:
        dto = schemas[protocol].model_validate(data)
    except ValidationError as error:
        unknown = any(e["type"] == "extra_forbidden" for e in error.errors())
        raise RkaiError(
            "unsupported_parameter" if unknown else "invalid_input",
            "Unsupported field or invalid protocol parameter",
            2,
        ) from error
    if protocol == "openai":
        if dto.max_completion_tokens is not None and "max_tokens" in data:
            raise RkaiError("invalid_input", "Use only one token limit field", 2)
        capability = "chat"
        payload = {
            "messages": [m.model_dump() for m in dto.messages],
            "max_tokens": dto.max_completion_tokens
            if dto.max_completion_tokens is not None
            else dto.max_tokens,
            "temperature": dto.temperature,
        }
    elif protocol == "anthropic":
        capability = "chat"
        messages = [{"role": m.role, "content": text(m.content)} for m in dto.messages]
        if dto.system is not None:
            messages.insert(0, {"role": "system", "content": text(dto.system)})
        payload = {
            "messages": messages,
            "max_tokens": dto.max_tokens,
            "temperature": dto.temperature,
        }
    elif protocol == "embeddings":
        capability = "embedding"
        payload = {"texts": [dto.input] if isinstance(dto.input, str) else dto.input}
    else:
        capability = dto.capability
        if set(dto.input) & set(dto.parameters):
            raise RkaiError("invalid_input", "Duplicate input and parameter fields", 2)
        payload = {**dto.input, **dto.parameters}
        if capability == "classification":
            if media is None or payload.pop("image", None) != "media:image":
                raise RkaiError("invalid_input", "Classification requires uploaded media:image", 2)
            payload["image"] = str(media)
        elif media is not None:
            raise RkaiError("unsupported_capability", "Uploaded media requires classification", 3)
    validate_request(capability, payload)
    return dto.model, capability, payload, getattr(dto, "stream", False)


def status(error):
    if error.code == "unknown_alias":
        return 404
    if error.code == "input_too_large":
        return 413
    if error.code == "request_timeout":
        return 504
    if error.code == "service_closing":
        return 503
    return {2: 400, 3: 422, 4: 429, 130: 503}.get(error.exit_code, 500)


def error_body(protocol, error):
    code = "model_not_found" if error.code == "unknown_alias" else error.code
    kind = {
        400: "invalid_request_error",
        404: "not_found_error",
        413: "invalid_request_error",
        422: "invalid_request_error",
        429: "overloaded_error",
    }.get(status(error), "api_error")
    safe = {"type": kind, "message": str(error), "code": code}
    return {"type": "error", "error": safe} if protocol == "anthropic" else {"error": safe}


def finish_reason(result, protocol):
    reason = result["finish_reason"]
    if reason == "unknown":
        return None
    return (
        {"stop": "end_turn", "length": "max_tokens"}[reason] if protocol == "anthropic" else reason
    )


def response(protocol, model, accepted, result):
    if protocol == "native":
        return {
            "request_id": accepted["request_id"],
            "plan": accepted["plan"],
            "device": accepted["device"],
            "capability_result": result,
            "status": "completed",
        }
    if protocol == "embeddings":
        return {
            "object": "list",
            "model": model,
            "data": [
                {"object": "embedding", "index": i, "embedding": v}
                for i, v in enumerate(result["vectors"])
            ],
        }
    if protocol == "anthropic":
        return {
            "id": "msg_" + accepted["request_id"],
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": result["text"]}],
            "stop_reason": finish_reason(result, protocol),
            "stop_sequence": None,
            "usage": {"input_tokens": None, "output_tokens": None},
        }
    return {
        "id": "chatcmpl-" + accepted["request_id"],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result["text"]},
                "finish_reason": finish_reason(result, protocol),
            }
        ],
    }


def sse(data, event=None):
    value = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, allow_nan=False)
    return ((f"event: {event}\n" if event else "") + f"data: {value}\n\n").encode()


class Stream:
    def __init__(self, protocol, model, accepted):
        self.protocol, self.model, self.accepted = protocol, model, accepted
        self.result = None
        self.created = int(time.time())
        self.sequence = -1

    def chunk(self, delta, reason=None):
        return sse(
            {
                "id": "chatcmpl-" + self.accepted["request_id"],
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            }
        )

    def named(self, kind, **fields):
        return sse({"type": kind, **fields}, kind)

    def map(self, event):
        kind = event["type"]
        self.sequence = event["sequence"]
        if self.protocol == "native":
            return [sse(event, kind)]
        if kind in {"failed", "cancelled"}:
            error = event.get("error") or {"code": "cancelled", "message": "Request cancelled"}
            err = RkaiError(error["code"], error["message"], error.get("exit_code", 1))
            return [
                sse(
                    error_body(self.protocol, err),
                    "error" if self.protocol == "anthropic" else None,
                )
            ]
        if kind == "accepted":
            if self.protocol == "openai":
                return [self.chunk({"role": "assistant", "content": ""})]
            initial = response(
                "anthropic", self.model, event, {"text": "", "finish_reason": "unknown"}
            )
            initial["content"] = []
            return [
                self.named("message_start", message=initial),
                self.named(
                    "content_block_start", index=0, content_block={"type": "text", "text": ""}
                ),
            ]
        if kind == "output":
            if event["kind"] == "result":
                self.result = event["payload"]
                return []
            delta = event["payload"]["text"]
            if self.protocol == "openai":
                return [self.chunk({"content": delta})]
            return [
                self.named(
                    "content_block_delta", index=0, delta={"type": "text_delta", "text": delta}
                )
            ]
        if kind == "completed":
            reason = finish_reason(self.result, self.protocol)
            if self.protocol == "openai":
                return [self.chunk({}, reason), sse("[DONE]")]
            return [
                self.named("content_block_stop", index=0),
                self.named(
                    "message_delta",
                    delta={"stop_reason": reason, "stop_sequence": None},
                    usage={"output_tokens": None},
                ),
                self.named("message_stop"),
            ]
        return []
