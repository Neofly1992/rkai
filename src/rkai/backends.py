"""Native context ownership. These implementations are imported only in workers."""

from typing import Annotated, Literal

from pydantic import Field, ValidationError

from .errors import RkaiError
from .manifest import Strict


class BackendConfig(Strict):
    memory_bytes: Annotated[int, Field(ge=1)]
    memory_evidence: Annotated[str, Field(min_length=1, max_length=512)]
    threads: Annotated[int, Field(ge=1, le=64)] = 1
    input_names: list[str] = []
    input_layout: Literal["nchw", "nhwc"] = "nchw"
    context_length: Annotated[int, Field(ge=16, le=32768)] = 2048


PROFILES = {
    "onnxruntime": "onnxruntime/1.23.2/cpu",
    "rknn": "rknn/2.3.2/rk3588",
    "rkllm": "rkllm/1.3.1/rk3588",
    "llama-cpp": "llama-cpp-python/0.3.16/cpu",
}
ACCELERATORS = {
    "onnxruntime": "cpu",
    "llama-cpp": "cpu",
    "rknn": "rockchip-npu",
    "rkllm": "rockchip-npu",
}
ENVIRONMENTS = {
    "onnxruntime": "onnx-cpu",
    "rknn": "rknn-runtime",
    "rkllm": "rkllm-runtime",
    "llama-cpp": "llama-cpu",
}


def configuration(component):
    if component.backend.id not in PROFILES:
        raise RkaiError("unsupported_backend", "Backend is not registered", 3)
    if component.compatibility != PROFILES[component.backend.id]:
        raise RkaiError("unsupported_compatibility", "Unknown backend compatibility profile", 3)
    if set(component.files) != {"model"}:
        raise RkaiError("invalid_backend", "Backend requires exactly one model role", 2)
    try:
        return BackendConfig.model_validate(component.config)
    except ValidationError as e:
        raise RkaiError(
            "invalid_backend", "Backend configuration requires a memory estimate/evidence", 2
        ) from e


class OnnxBackend:
    def __init__(self, path, config, runtime=None, adapter=None):
        import onnxruntime as ort

        if ort.__version__ != "1.23.2":
            raise RkaiError("runtime_version", "Expected ONNX Runtime 1.23.2", 3)
        options = ort.SessionOptions()
        options.intra_op_num_threads = config.threads
        options.inter_op_num_threads = 1
        options.log_severity_level = 4
        self.context = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.evidence = {"runtime": ort.__version__, "provider": "CPUExecutionProvider"}

    def infer(self, inputs):
        return self.context.run(None, inputs)

    def close(self):
        self.context = None


class LlamaBackend:
    def __init__(self, path, config, runtime=None, adapter=None):
        import llama_cpp

        if llama_cpp.__version__ != "0.3.16":
            raise RkaiError("runtime_version", "Expected llama-cpp-python 0.3.16", 3)
        self.config = config
        self.context = llama_cpp.Llama(
            model_path=str(path),
            n_ctx=config.context_length,
            n_threads=config.threads,
            n_gpu_layers=0,
            verbose=False,
        )
        self.evidence = {"runtime": llama_cpp.__version__, "provider": "cpu"}

    def token_count(self, prompt):
        return len(self.context.tokenize(prompt.encode(), add_bos=True, special=True))

    def generate(self, prompt, request, cancel):
        # Full session history is adapter input; no implicit state or replay after errors.
        self.context.reset()
        chunks = self.context.create_completion(
            prompt=prompt,
            max_tokens=request["max_tokens"],
            temperature=request["temperature"],
            stream=True,
        )
        text = ""
        reason = None
        try:
            for chunk in chunks:
                if cancel.is_set():
                    return
                choice = chunk["choices"][0]
                delta = choice.get("text", "")
                if delta:
                    text += delta
                    yield "delta", {"text": delta}
                if choice.get("finish_reason"):
                    reason = choice["finish_reason"]
        finally:
            chunks.close()
        if reason not in ("stop", "length"):
            raise RkaiError("invalid_output", "Missing or unsupported chat finish reason")
        yield "result", {"text": text, "finish_reason": reason}

    def close(self):
        if self.context is not None:
            self.context.close()
            self.context = None


BACKENDS = {"onnxruntime": OnnxBackend, "llama-cpp": LlamaBackend}
