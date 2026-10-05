"""Real public CPU models. No result here is RK3588/NPU acceptance evidence."""

import argparse
import ast
import hashlib
import json
import math
import tempfile
import time
from pathlib import Path

from rkai.config import Config
from rkai.conversion import convert
from rkai.download import download
from rkai.environments import host, prepare, resolve
from rkai.execution import Session
from rkai.manifest import File
from rkai.sources import client
from rkai.storage import Library

MODELS = [
    (
        "mobilenet",
        "onnxmodelzoo/mobilenetv2-12",
        "23a2a25eaaa6fcd61865414c8a1acf13dda66b7a",
        {
            "model": (
                "mobilenetv2-12.onnx",
                13964571,
                "c0c3f76d93fa3fd6580652a45618618a220fced18babf65774ed169de0432ad5",
            ),
        },
    ),
    (
        "minilm",
        "sentence-transformers/all-MiniLM-L6-v2",
        "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
        {
            "model": (
                "onnx/model.onnx",
                90405214,
                "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
            ),
            "tokenizer": (
                "tokenizer.json",
                466247,
                "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
            ),
        },
    ),
    (
        "stories",
        "ggml-org/models",
        "499bc8821c6b12b4e53c5bffcb21ec206f212d81",
        {
            "model": (
                "tinyllamas/stories260K.gguf",
                1185376,
                "270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d",
            ),
        },
    ),
]
ROCKCHIP = "https://raw.githubusercontent.com/airockchip/rknn-toolkit2/59a913d172e7f5ff03c9076e2ec7b1b1288ffd08/rknn-toolkit-lite2/examples/resnet18/"


def measure(path, relative):
    raw = path.read_bytes()
    return {"path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def run(root, data_dir):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    result = {"device": "cpu", "npu_execution": "not_run", "models": [], "host": list(host())}
    with client() as http, Library(Config(data_dir=str(data_dir.resolve()))) as library:
        result["environments"] = {}
        for kind in ("onnx-cpu", "llama-cpu"):
            prepare(library, kind)
            ready = json.loads((resolve(library, kind) / "ready.json").read_text())
            result["environments"][kind] = {
                k: ready[k] for k in ("lock", "python", "packages", "preparation_probe")
            }
        for name, repository, revision, files in MODELS:
            source = root / name
            source.mkdir(exist_ok=True)
            inputs = {}
            for key, (path, size, sha) in files.items():
                spec = File(path=path, size=size, sha256=sha)
                download(
                    http,
                    [f"https://huggingface.co/{repository}/resolve/{revision}/{path}"],
                    source / path,
                    spec,
                    root / "transfers",
                )
                inputs[key] = spec.model_dump(exclude_none=True)
            if name == "mobilenet":
                spec = File(
                    path="space_shuttle_224.jpg",
                    size=23472,
                    sha256="a827cc9060d15cd34b1b5ac512f4d6d8366416a5874a2075951991cbacef78d4",
                )
                download(http, [ROCKCHIP + spec.path], source / spec.path, spec, root / "transfers")
                response = http.get(ROCKCHIP + "synset_label.py")
                response.raise_for_status()
                tree = ast.parse(response.text)
                labels = next(
                    ast.literal_eval(node.value)
                    for node in tree.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "labels" for t in node.targets)
                )
                if isinstance(labels, dict):
                    labels = [labels[i] for i in range(len(labels))]
                assert len(labels) == 1000 and all(isinstance(label, str) for label in labels)
                (source / "labels.txt").write_text("\n".join(labels) + "\n")
                inputs["labels"] = measure(source / "labels.txt", "labels.txt")
                capability, adapter, cfg = (
                    "classification",
                    "image-classification",
                    {"input_name": "input", "labels": "file:labels"},
                )
                request = {"image": str(source / spec.path), "top_k": 5}
            elif name == "minilm":
                capability, adapter, cfg = (
                    "embedding",
                    "sentence-embedding",
                    {"tokenizer": "file:tokenizer"},
                )
                request = {
                    "texts": [
                        "The cat sits outside.",
                        "A cat is sitting outdoors.",
                        "A rocket flies into space.",
                    ]
                }
            else:
                capability, adapter, cfg = "chat", "gguf-chat", {"prompt_format": "plain"}
                request = {
                    "messages": [{"role": "user", "content": "Once upon a time"}],
                    "max_tokens": 32,
                    "temperature": 0.0,
                }
            backend = "llama-cpp" if name == "stories" else "onnxruntime"
            profile = (
                "llama-cpp-python/0.3.16/cpu" if name == "stories" else "onnxruntime/1.23.2/cpu"
            )
            os_name, arch = host()
            recipe = {
                "schema": "rkai.recipe/v0",
                "id": name,
                "model": {"id": "verification/" + name, "revision": revision},
                "converter": "gguf-import" if name == "stories" else "onnx-import",
                "inputs": inputs,
                "output_plan": {
                    "id": "cpu",
                    "target": {"os": os_name, "arch": arch, "accelerator": "cpu"},
                    "capabilities": [{"id": capability, "version": 1}],
                    "default_capability": capability,
                    "adapter": {"id": adapter, "api": 1, "config": cfg},
                    "components": [
                        {
                            "id": "model",
                            "backend": {"id": backend, "api": 1},
                            "compatibility": profile,
                            "files": {"model": "model"},
                            "config": {
                                "memory_bytes": 512 * 1024 * 1024,
                                "memory_evidence": (
                                    "conservative CPU verification admission allowance; "
                                    "not measured NPU peak"
                                ),
                                "context_length": 1024 if name == "stories" else 512,
                            },
                        }
                    ],
                },
            }
            recipe_path = root / (name + ".recipe.json")
            recipe_path.write_text(json.dumps(recipe, indent=2))
            # Exercise real source resolver/fixed snapshot as well as local raw model input.
            ref = f"hf://{repository}@{revision}" if name == "minilm" else str(source)
            installed = convert(library, ref, str(recipe_path), alias="verify/" + name)
            with Session(library, "verify/" + name, allow_cpu=True) as session:
                events = list(session.infer(request))
                assert events[-1]["type"] == "completed", events[-1]
                output = next(e["payload"] for e in events if e.get("kind") == "result")
                evidence = {
                    "name": name,
                    "repository": repository,
                    "revision": revision,
                    "input_files": inputs,
                    "runtime": session.evidence,
                    "conversion_operation": installed["conversion_operation_id"],
                    "event_types": [e["type"] for e in events],
                }
                receipt = json.loads(
                    (
                        library.root
                        / "installations"
                        / installed["installation"]
                        / "conversion-record.json"
                    ).read_text()
                )
                evidence["conversion"] = {
                    k: receipt[k]
                    for k in (
                        "toolchain_lock",
                        "converter_implementation_sha256",
                        "conversion_evidence",
                        "input_snapshot",
                    )
                }
                if name == "mobilenet":
                    assert output["labels"][0]["index"] == 812, output
                    evidence["top_k"] = output["labels"]
                elif name == "minilm":
                    vectors = output["vectors"]
                    similarities = [
                        sum(a * b for a, b in zip(vectors[0], other, strict=True))
                        for other in vectors[1:]
                    ]
                    assert output["dimension"] == 384 and similarities[0] > similarities[1]
                    assert all(
                        math.isclose(sum(x * x for x in v), 1.0, abs_tol=1e-5) for v in vectors
                    )
                    evidence.update(dimension=384, cosine_similarities=similarities)
                else:
                    assert output["text"] and any(e.get("kind") == "delta" for e in events)
                    assert session.history[-1]["content"] == output["text"]
                    session.reset()
                    assert session.history == []
                    evidence.update(
                        text=output["text"],
                        finish_reason=output["finish_reason"],
                        semantics="plain text continuation; not instruction/chat quality",
                    )
                    stream = session.infer({**request, "max_tokens": 256})
                    for event in stream:
                        if event.get("kind") == "delta":
                            session.cancel()
                            break
                    remaining = list(stream)
                    assert remaining[-1]["type"] == "cancelled", remaining[-1]
                    assert session.closed and session.process.poll() is not None
                    evidence["stream_cancel"] = "passed after output; no replay"
                assert library.inspect("verify/" + name)["execution_validation"] == "unverified"
                result["models"].append(evidence)
            if name == "stories":
                import rkai.execution as execution

                old_timeout, old_events = execution.IO_TIMEOUT_SECONDS, execution.QUEUE_EVENTS
                try:
                    # Scale only parent queue/deadline; native model and worker remain real.
                    execution.IO_TIMEOUT_SECONDS, execution.QUEUE_EVENTS = 0.1, 4
                    with Session(library, "verify/stories", allow_cpu=True) as blocked:
                        stream = blocked.infer({**request, "max_tokens": 256})
                        assert next(stream)["type"] == "accepted"
                        deadline = time.monotonic() + 10
                        while not blocked.closed and time.monotonic() < deadline:
                            time.sleep(0.02)
                        terminal = list(stream)[-1]
                        assert terminal["type"] == "failed", terminal
                        assert terminal["error"]["code"] == "backpressure", terminal
                        assert blocked.closed and blocked.process.poll() is not None
                        assert blocked.history == []
                        evidence["slow_consumer"] = (
                            "real native stream reaped; scaled queue=4, drain deadline=0.1s"
                        )
                finally:
                    execution.IO_TIMEOUT_SECONDS, execution.QUEUE_EVENTS = old_timeout, old_events
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="rkai-m2-public-") as tmp:
        root = args.work_dir or Path(tmp) / "sources"
        result = run(root, args.data_dir or Path(tmp) / "library")
        raw = json.dumps(result, indent=2)
        if args.evidence:
            args.evidence.write_text(raw + "\n")
        print(raw)


if __name__ == "__main__":
    main()
