"""Conversion uses the existing acquisition/commit authority for immutable inputs and outputs."""

import json
import os
import shutil
import subprocess
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from .adapters import configuration as adapter_configuration
from .backends import configuration as backend_configuration
from .download import atomic_write
from .environments import host, prepare, recipe_lock, resolve
from .errors import RkaiError
from .files import ensure_directory, open_file, read_file, remove_tree
from .ipc import receive, send
from .manifest import (
    MAX_MANIFEST_BYTES,
    File,
    HubSource,
    Manifest,
    Model,
    Plan,
    Strict,
    digest,
    no_duplicates,
    reject_constant,
)
from .processes import handshake, launch_worker, stop
from .sources import Provider, client, resolve_reference
from .storage import defer_interrupts, lock

CONVERSION_TIMEOUT_SECONDS = 3600
MAX_CALIBRATION_TENSOR_BYTES = 64 * 1024 * 1024


class Parameters(Strict):
    quantize: bool = False
    # Each row is one sample; columns follow the compiled graph's input order.
    calibration: list[Annotated[list[str], Field(min_length=1)]] = []
    quantized_dtype: Literal["W8A8", "W4A16"] = "W8A8"
    input_names: list[str] = []
    input_shapes: list[list[Annotated[int, Field(ge=1)]]] = []

    @model_validator(mode="after")
    def calibration_required(self):
        if self.quantize and not self.calibration:
            raise ValueError("quantization requires declared calibration files")
        if bool(self.input_names) != bool(self.input_shapes) or len(self.input_names) != len(
            self.input_shapes
        ):
            raise ValueError("input names and shapes must match")
        return self


class Recipe(Strict):
    schema_version: Literal["rkai.recipe/v0"] = Field(alias="schema")
    id: str
    model: Model
    converter: Literal["onnx-import", "onnx-rknn", "gguf-import", "hf-rkllm"]
    inputs: dict[str, File]
    parameters: Parameters = Field(default_factory=Parameters)
    output_plan: Plan

    @model_validator(mode="after")
    def supported(self):
        if "model" not in self.inputs:
            raise ValueError("model input required")
        for spec in self.inputs.values():
            if spec.location is not None:
                raise ValueError("input locations are resolved from the supplied REF")
        # Reuse manifest path/collision/reference validation rather than a second rule.
        raw_plan = input_plan(self.inputs)
        Manifest.model_validate(
            {
                "schema": "rkai.model/v0",
                "model": self.model.model_dump(),
                "sources": {},
                "files": {k: v.model_dump() for k, v in self.inputs.items()},
                "plans": [raw_plan],
            }
        )
        if (
            "conversion-record" in self.inputs
            or self.output_plan.adapter.config.get("conversion_record") is not None
        ):
            raise ValueError("conversion-record is generated and reserved")
        if Manifest.closure(self.output_plan) - self.inputs.keys():
            raise ValueError("output assets must be declared inputs")
        if {name for row in self.parameters.calibration for name in row} - self.inputs.keys():
            raise ValueError("calibration input is not declared")
        if self.parameters.calibration and not self.parameters.quantize:
            raise ValueError("calibration requires quantization")
        if self.parameters.quantize and self.converter == "onnx-rknn":
            if self.parameters.quantized_dtype != "W8A8":
                raise ValueError("RK3588 converter currently supports W8A8 quantization only")
            names = self.output_plan.components[0].config.get("input_names", [])
            if not names or len(set(names)) != len(names):
                raise ValueError("quantization requires explicit distinct RKNN input names")
            if any(len(row) != len(names) for row in self.parameters.calibration):
                raise ValueError("calibration columns must match RKNN input order")
            if self.parameters.input_names and self.parameters.input_names != names:
                raise ValueError("conversion and runtime input order must agree")
            if self.output_plan.adapter.id == "image-classification" and len(names) != 1:
                raise ValueError("image calibration requires one input")
            if self.output_plan.adapter.id == "sentence-embedding" and any(
                not self.inputs[name].path.endswith(".npy")
                for row in self.parameters.calibration
                for name in row
            ):
                raise ValueError("embedding calibration requires pre-tokenized NPY tensors")
        if (
            self.parameters.quantize
            and self.converter == "hf-rkllm"
            and (len(self.parameters.calibration) != 1 or len(self.parameters.calibration[0]) != 1)
        ):
            raise ValueError("RKLLM requires one JSON calibration dataset")
        return self


def input_plan(files):
    os_name, arch = host()
    return {
        "id": "source",
        "target": {"os": os_name, "arch": arch, "accelerator": "cpu"},
        "capabilities": [{"id": "source", "version": 1}],
        "default_capability": "source",
        "adapter": {"id": "source-snapshot", "api": 1, "config": {}},
        "components": [
            {
                "id": "source",
                "backend": {"id": "source", "api": 1},
                "compatibility": "source-only/not-executable",
                "files": {name: name for name in files},
            }
        ],
    }


def read_recipe(path):
    raw = read_file(Path(path).expanduser().resolve(), MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise RkaiError("invalid_recipe", "Recipe exceeds 4 MiB", 2)
    try:
        recipe = Recipe.model_validate(
            json.loads(raw, object_pairs_hook=no_duplicates, parse_constant=reject_constant)
        )
    except (ValueError, RecursionError, ValidationError) as e:
        raise RkaiError("invalid_recipe", "Invalid or unsupported conversion recipe", 2) from e
    return recipe, raw


def convert(library, ref, recipe_path, registry=None, output=None, alias=None):
    recipe, raw = read_recipe(recipe_path)
    kind = {
        "onnx-import": "onnx-cpu",
        "onnx-rknn": "rknn-convert",
        "gguf-import": "llama-cpu",
        "hf-rkllm": "rkllm-convert",
    }[recipe.converter]
    dependencies, assets, environment_hash = recipe_lock(kind)  # Host check before acquisition.
    from .execution import contract

    cfg, _, _ = contract(recipe.output_plan, recipe.output_plan.default_capability)
    expected = {
        "onnx-import": "onnxruntime",
        "onnx-rknn": "rknn",
        "gguf-import": "llama-cpp",
        "hf-rkllm": "rkllm",
    }[recipe.converter]
    if len(recipe.output_plan.components) != 1:
        raise RkaiError("unsupported_conversion", "This converter generates one graph component", 3)
    component = recipe.output_plan.components[0]
    backend_configuration(component)
    if (
        component.backend.id != expected
        or component.files != {"model": "model"}
        or cfg.component != component.id
    ):
        raise RkaiError("invalid_recipe", "Output backend/roles do not match converter", 2)
    if (
        recipe.converter in ("onnx-rknn", "hf-rkllm")
        and recipe.output_plan.target.accelerator != "rockchip-npu"
    ):
        raise RkaiError("invalid_recipe", "RKNN output must target RK3588 NPU", 2)
    if (
        recipe.converter not in ("onnx-rknn", "hf-rkllm")
        and recipe.output_plan.target.accelerator != "cpu"
    ):
        raise RkaiError("invalid_recipe", "CPU converter requires an independent CPU artifact", 2)
    if recipe.converter not in ("onnx-rknn", "hf-rkllm") and (
        recipe.parameters.quantize or recipe.parameters.calibration
    ):
        raise RkaiError("invalid_recipe", "Import converters do not perform quantization", 2)
    destination = None
    if output:
        path = Path(output).expanduser().absolute()
        destination = path.parent.resolve() / path.name
        if destination.exists() or destination.is_symlink():
            raise RkaiError("output_exists", "--output must be a new directory", 2)
    operation = uuid.uuid4().hex
    workspace = library.root / "conversions" / operation
    record = {
        "operation_id": operation,
        "state": "inspecting",
        "recipe_sha256": digest(raw),
        "converter_implementation_sha256": digest(read_file(Path(__file__).resolve())),
        "recipe": recipe.model_dump(by_alias=True),
        "toolchain_lock": environment_hash,
        "host": list(host()),
        "execution_validation": "unverified",
    }
    record_path = workspace / "record.json"

    def state(name, **changes):
        record.update(state=name, **changes)
        atomic_write(record_path, json.dumps(record, allow_nan=False).encode())

    with (
        lock(library.root / ("locks/conversion-" + operation)) as operation_fd,
        ExitStack() as owned,
    ):
        retain_fds = [operation_fd]
        ensure_directory(workspace)
        state("preparing")
        try:
            sources, files, snapshot = source_manifest(library, ref, recipe, registry)
            prepare(library, kind)
            environment = resolve(library, kind)
            input_alias = "conversion-input/" + operation
            raw_input = json.dumps(
                {
                    "schema": "rkai.model/v0",
                    "model": {
                        "id": "conversion-input/" + digest(raw),
                        "revision": digest(json.dumps(snapshot, sort_keys=True).encode()),
                    },
                    "sources": sources,
                    "files": files,
                    "plans": [input_plan(files)],
                }
            ).encode()
            installed = library.acquire_manifest(
                raw_input, snapshot, snapshot.get("local_root"), plan_id="source", alias=input_alias
            )
            identity = owned.enter_context(library.lease(input_alias, retain_fds))
            input_root = library.root / "installations" / identity
            state(
                "converting",
                input_snapshot={
                    k: v for k, v in installed["snapshot"].items() if k != "local_root"
                },
                input_installation=identity,
            )
            tree = workspace / "tree"
            ensure_directory(tree)
            extension = {
                "onnx-import": ".onnx",
                "onnx-rknn": ".rknn",
                "gguf-import": ".gguf",
                "hf-rkllm": ".rkllm",
            }[recipe.converter]
            output_model = tree / ("model" + extension)
            evidence = conversion_worker(environment, recipe, input_root, output_model, retain_fds)
            if library.inspect(input_alias)["file_state"] != "ready":
                raise RkaiError(
                    "conversion_input_changed",
                    "Input snapshot changed during compilation; output rejected",
                )
            state("verifying", conversion_evidence=evidence)
            generated = {"model": measure(output_model, output_model.name)}
            for name, spec in recipe.inputs.items():
                if name in Manifest.closure(recipe.output_plan) - {"model"}:
                    ensure_directory((tree / spec.path).parent)
                    library._copy_file(input_root / spec.path, tree / spec.path, spec.size)
                    generated[name] = spec.model_dump(exclude_none=True)
            record["outputs"] = generated.copy()
            record["state"] = "converted-unverified"
            record_raw = json.dumps(record, allow_nan=False).encode()
            atomic_write(tree / "conversion-record.json", record_raw)
            generated["conversion-record"] = measure(
                tree / "conversion-record.json", "conversion-record.json"
            )
            plan = recipe.output_plan.model_copy(deep=True)
            plan.adapter.config["conversion_record"] = "file:conversion-record"
            manifest_raw = json.dumps(
                {
                    "schema": "rkai.model/v0",
                    "model": {**recipe.model.model_dump(), "revision": digest(record_raw)},
                    "sources": {},
                    "files": generated,
                    "plans": [plan.model_dump()],
                }
            ).encode()
            from .manifest import parse

            manifest = parse(manifest_raw)
            for name in manifest.closure(plan):
                from .download import verify

                if not verify(tree / manifest.files[name].path, manifest.files[name]):
                    raise RkaiError("conversion_corrupt", "Generated artifact failed validation")
            atomic_write(tree / "manifest.json", manifest_raw)
            # Inputs remain leased until conversion process has actually exited.
            state(
                "committing",
                output_manifest_hash=digest(manifest_raw),
                output_plan=plan.id,
                alias=alias or manifest.model.id,
                destination=str(destination) if destination else None,
            )
            if destination:
                with defer_interrupts() as cancellation:
                    state("committing")
                    # Stage and rename on the destination filesystem; never expose a partial bundle.
                    temporary = destination.with_name(destination.name + ".rkai-" + operation)
                    ensure_directory(temporary)
                    try:
                        for name in manifest.closure(plan):
                            spec = manifest.files[name]
                            ensure_directory((temporary / spec.path).parent)
                            library._copy_file(tree / spec.path, temporary / spec.path, spec.size)
                        atomic_write(temporary / "manifest.json", manifest_raw)
                        from .files import move_directory

                        if destination.exists() or destination.is_symlink():
                            raise RkaiError(
                                "output_exists", "Output appeared during conversion; preserved", 2
                            )
                        move_directory(temporary, destination)
                    finally:
                        if temporary.exists():
                            remove_tree(temporary)
                    state("completed", output=str(destination))
                    result = {
                        "operation_id": operation,
                        "state": "completed",
                        "output": str(destination),
                        "execution_validation": "unverified",
                        "cancellation_deferred": cancellation["requested"],
                    }
            else:
                # The library protects its own commit; keep the outer conversion
                # receipt consistent too when SIGINT arrives just after that commit.
                with defer_interrupts() as cancellation:
                    state("committing")
                    result = library.acquire_manifest(
                        manifest_raw,
                        {
                            "kind": "conversion",
                            "recipe_sha256": digest(raw),
                            "toolchain_lock": environment_hash,
                        },
                        str(tree),
                        plan_id=plan.id,
                        alias=alias,
                    )
                    state(
                        "completed",
                        installation=result["installation"],
                        install_operation=result["operation_id"],
                    )
                    result["conversion_operation_id"] = operation
                    if cancellation["requested"]:
                        result["cancellation_deferred"] = True
            return result
        except KeyboardInterrupt as e:
            with defer_interrupts():
                committed = committed_output(library, record)
                if committed is not None:
                    state("completed", error=None, recovered_after_interrupt=True)
                    committed["cancellation_deferred"] = True
                    committed["conversion_operation_id"] = operation
                    return committed
            state("cancelled", error="interrupted")
            raise RkaiError("interrupted", f"Conversion stopped; record {operation}", 130) from e
        except (RkaiError, OSError) as e:
            state("failed", error=e.code if isinstance(e, RkaiError) else "storage_error")
            raise
        finally:
            owned.close()
            input_alias = "conversion-input/" + operation
            if library.db.execute("SELECT 1 FROM aliases WHERE name=?", (input_alias,)).fetchone():
                library.delete(input_alias)


def source_manifest(library, ref, recipe, registry):
    path = Path(ref).expanduser()
    if path.exists() or ref.startswith(("/", "./", "../")):
        if registry:
            raise RkaiError("source_conflict", "Local source conflicts with --registry", 2)
        path = path.parent.resolve() / path.name
        if path.is_file():
            if path.name != recipe.inputs["model"].path:
                raise RkaiError(
                    "invalid_recipe", "Local model filename must match recipe model path", 2
                )
            path = path.parent
        return (
            {},
            {k: v.model_dump(exclude_none=True) for k, v in recipe.inputs.items()},
            {
                "kind": "local",
                "local_root": str(path),
                "input_digests": {k: v.sha256 for k, v in recipe.inputs.items()},
            },
        )
    reference = resolve_reference(ref, registry, library.config)
    settings = library.config.registries[reference.registry]
    if settings.kind == "https":
        raise RkaiError(
            "unsupported_source",
            "Raw conversion refs use HF/MS; static HTTPS uses local recipe snapshot",
            3,
        )
    sources, files, revisions = {}, {}, {}
    selector = reference.selector
    with client() as http:
        provider = Provider(http, settings)
        for name, spec in recipe.inputs.items():
            revision, _ = provider.fixed_file(reference.repository, selector, spec.path)
            if settings.kind == "huggingface":
                selector = revision  # One immutable repo commit for all HF inputs.
            sources[name] = HubSource(
                kind=settings.kind,
                origin=settings.origin,
                repository=reference.repository,
                revision=revision,
            ).model_dump()
            files[name] = {
                **spec.model_dump(exclude_none=True),
                "location": {"source": name, "path": spec.path},
            }
            revisions[name] = revision
    return (
        sources,
        files,
        {
            "kind": settings.kind,
            "origin": settings.origin,
            "registry": reference.registry,
            "repository": reference.repository,
            "selector": reference.selector,
            "file_revisions": revisions,
        },
    )


def measure(path, relative):
    import hashlib

    hasher = hashlib.sha256()
    size = 0
    with open_file(path) as f:
        while chunk := f.read(1024 * 1024):
            size += len(chunk)
            hasher.update(chunk)
    if size == 0:
        raise RkaiError("conversion_empty", "Converter generated an empty file")
    return File(path=relative, size=size, sha256=hasher.hexdigest()).model_dump(exclude_none=True)


def conversion_worker(environment, recipe, root, output, retain_fds):
    process, parent, death_fd = launch_worker(environment / "bin/python", retain_fds)
    try:
        handshake(process, parent, 120)
        send(
            parent,
            {
                "type": "convert",
                "recipe": recipe.model_dump(by_alias=True),
                "root": str(root),
                "output": str(output),
            },
        )
        response = receive(parent, CONVERSION_TIMEOUT_SECONDS)
        process.wait(timeout=10)
        if response.get("type") != "converted" or process.returncode != 0:
            error = response.get("error", {})
            raise RkaiError(
                "conversion_failed",
                "Converter failed; no artifact installed",
                error.get("exit_code", 1),
                details={
                    "worker_code": error.get("code", "unknown"),
                    "diagnostics": process.diagnostics.finish(),
                    "worker_details": error.get("details", {}),
                },
            )
        return response["evidence"]
    except (EOFError, subprocess.TimeoutExpired) as e:
        raise RkaiError("converter_crashed", "Converter disconnected or did not exit") from e
    finally:
        parent.close()
        os.close(death_fd)
        stop(process)


def execute_conversion(recipe_data, root, output):
    import importlib.metadata

    recipe = Recipe.model_validate(recipe_data)
    model = root / recipe.inputs["model"].path
    if recipe.converter in ("onnx-import", "onnx-rknn"):
        import onnx

        graph = onnx.load(str(model), load_external_data=False)

        def tensors(graph):
            yield from graph.initializer
            for node in graph.node:
                for attr in node.attribute:
                    if attr.type == onnx.AttributeProto.TENSOR:
                        yield attr.t
                    elif attr.type == onnx.AttributeProto.TENSORS:
                        yield from attr.tensors
                    elif attr.type == onnx.AttributeProto.GRAPH:
                        yield from tensors(attr.g)
                    elif attr.type == onnx.AttributeProto.GRAPHS:
                        for nested in attr.graphs:
                            yield from tensors(nested)

        if any(t.data_location == onnx.TensorProto.EXTERNAL for t in tensors(graph.graph)):
            raise RkaiError(
                "unsupported_onnx", "ONNX external tensor data is not supported; export inline", 3
            )
        onnx.checker.check_model(graph)
    if recipe.converter == "onnx-import":
        shutil.copyfile(model, output)
        import onnxruntime as ort

        context = ort.InferenceSession(str(output), providers=["CPUExecutionProvider"])
        evidence = {
            "graph_check": "passed",
            "cpu_load": "passed",
            "runtime": ort.__version__,
            "inputs": [i.name for i in context.get_inputs()],
        }
        del context
        return evidence
    if recipe.converter == "gguf-import":
        with open_file(model) as f:
            if f.read(4) != b"GGUF":
                raise RkaiError("unsupported_gguf", "Source is not a GGUF model", 3)
        shutil.copyfile(model, output)
        from .backends import LlamaBackend, configuration

        backend = LlamaBackend(
            output,
            configuration(recipe.output_plan.components[0]),
            adapter=adapter_configuration(recipe.output_plan.adapter),
        )
        evidence = {"cpu_load": "passed", **backend.evidence}
        backend.close()
        return evidence
    if recipe.converter == "hf-rkllm":
        from rkllm.api import RKLLM

        if importlib.metadata.version("rkllm-toolkit") != "1.3.1":
            raise RkaiError("toolchain_version", "Expected RKLLM Toolkit 1.3.1", 3)
        # SDK reads only the immutable local snapshot. Remote Python/custom auto_map is rejected.
        config = json.loads(read_file(root / "config.json", MAX_MANIFEST_BYTES))
        if (
            not isinstance(config, dict)
            or "auto_map" in config
            or any(f.path.endswith(".py") for f in recipe.inputs.values())
        ):
            raise RkaiError("unsupported_hf_architecture", "Custom model code is not allowed", 3)
        native = RKLLM()

        def check(code):
            if code != 0:
                raise RkaiError("converter_native_error", f"RKLLM converter returned {code}")

        try:
            check(
                native.load_huggingface(
                    model=str(root),
                    model_lora=None,
                    device="cpu",
                    dtype="float32",
                    custom_config=None,
                    load_weight=True,
                )
            )
            dataset = None
            if recipe.parameters.quantize:
                dataset = str(root / recipe.inputs[recipe.parameters.calibration[0][0]].path)
            check(
                native.build(
                    do_quantization=recipe.parameters.quantize,
                    optimization_level=1,
                    quantized_dtype=recipe.parameters.quantized_dtype,
                    quantized_algorithm="normal"
                    if recipe.parameters.quantized_dtype == "W8A8"
                    else "grq",
                    target_platform="RK3588",
                    num_npu_core=3,
                    extra_qparams=None,
                    dataset=dataset,
                    hybrid_rate=0,
                    max_context=recipe.output_plan.components[0].config["context_length"],
                )
            )
            check(native.export_rkllm(str(output)))
            return {
                "compiler": "rkllm-toolkit/1.3.1",
                "compiled": "passed",
                "npu_execution": "not_run",
            }
        finally:
            # Toolkit object owns compiler resources; the owned process exits before commit.
            del native
    from rknn.api import RKNN

    if importlib.metadata.version("rknn-toolkit2") != "2.3.2":
        raise RkaiError("toolchain_version", "Expected RKNN Toolkit2 2.3.2", 3)
    native = RKNN(verbose=False)
    try:
        native.config(
            target_platform="rk3588", quantized_dtype=recipe.parameters.quantized_dtype.lower()
        )
        kwargs = {}
        if recipe.parameters.input_names:
            kwargs = {
                "inputs": recipe.parameters.input_names,
                "input_size_list": recipe.parameters.input_shapes,
            }

        def check(code):
            if code != 0:
                raise RkaiError("converter_native_error", f"RKNN converter returned {code}")

        check(native.load_onnx(model=str(model), **kwargs))
        dataset = None
        if recipe.parameters.quantize:
            dataset = calibration_dataset(recipe, root, output.parent, graph)
        check(
            native.build(
                do_quantization=recipe.parameters.quantize,
                dataset=str(dataset) if dataset else None,
            )
        )
        check(native.export_rknn(str(output)))
        return {"compiler": "rknn-toolkit2/2.3.2", "compiled": "passed", "npu_execution": "not_run"}
    finally:
        native.release()


def load_calibration_tensor(path):
    import math

    import numpy as np

    try:
        with open_file(path) as stream:
            if os.fstat(stream.fileno()).st_size > MAX_CALIBRATION_TENSOR_BYTES:
                raise RkaiError("input_too_large", "Calibration tensor exceeds 64 MiB", 2)
            # Validate the header before np.load can allocate an attacker-sized shape.
            version = np.lib.format.read_magic(stream)
            if version not in {(1, 0), (2, 0)}:
                raise RkaiError("invalid_recipe", "Unsupported calibration NPY version", 2)
            reader = (
                np.lib.format.read_array_header_1_0
                if version == (1, 0)
                else np.lib.format.read_array_header_2_0
            )
            shape, _, dtype = reader(stream)
            if (
                dtype.hasobject
                or any(d <= 0 for d in shape)
                or math.prod(shape) * dtype.itemsize > MAX_CALIBRATION_TENSOR_BYTES
            ):
                raise RkaiError("invalid_recipe", "Unsafe calibration tensor header", 2)
            stream.seek(0)
            return np.load(stream, allow_pickle=False)
    except (ValueError, OSError, EOFError) as error:
        raise RkaiError(
            "invalid_recipe", "Calibration NPY is unreadable or malformed", 2
        ) from error


def calibration_dataset(recipe, root, workspace, graph):
    """SDK dataset rows contain normalized tensors, never unprocessed image paths."""
    import numpy as np

    from .adapters import ImageConfig, image_tensor

    cfg = adapter_configuration(recipe.output_plan.adapter)
    names = recipe.output_plan.components[0].config["input_names"]
    inputs = {value.name: value for value in graph.graph.input}
    shapes = dict(zip(recipe.parameters.input_names, recipe.parameters.input_shapes, strict=True))
    directory = workspace / "calibration"
    ensure_directory(directory)
    rows = []
    supported_types = {1: np.dtype("float32"), 6: np.dtype("int32"), 7: np.dtype("int64")}
    for sample, row in enumerate(recipe.parameters.calibration):
        paths = []
        for column, (name, asset) in enumerate(zip(names, row, strict=True)):
            source = root / recipe.inputs[asset].path
            if isinstance(cfg, ImageConfig):
                if cfg.input_name != name:
                    raise RkaiError("invalid_recipe", "Adapter/calibration input name differs", 2)
                tensor = image_tensor(source, cfg)
            else:
                tensor = load_calibration_tensor(source)
            if name not in inputs:
                raise RkaiError("invalid_recipe", "Calibration input not present in graph", 2)
            info = inputs[name].type.tensor_type
            expected = shapes.get(name) or [d.dim_value or None for d in info.shape.dim]
            if (
                tensor.dtype != supported_types.get(info.elem_type)
                or tensor.ndim != len(expected)
                or any(
                    size is not None and size != actual
                    for size, actual in zip(expected, tensor.shape, strict=True)
                )
                or not np.isfinite(tensor).all()
            ):
                raise RkaiError(
                    "invalid_recipe", "Calibration dtype/shape/values differ from graph", 2
                )
            path = directory / f"{sample}-{column}.npy"
            with open_file(path, "wb") as stream:
                np.save(stream, tensor, allow_pickle=False)
            if any(character.isspace() for character in str(path)):
                raise RkaiError(
                    "unsupported_calibration_path", "SDK dataset paths cannot contain spaces", 3
                )
            paths.append(str(path))
        rows.append(" ".join(paths))
    dataset = workspace / "calibration.txt"
    atomic_write(dataset, ("\n".join(rows) + "\n").encode())
    return dataset


def committed_output(library, record):
    """Read-only proof shared by live interruption and stopped-owner recovery."""
    if record["state"] not in {"committing", "completed"} or not record.get("output_manifest_hash"):
        return None
    destination = record.get("destination")
    identity = digest((record["output_manifest_hash"] + ":" + record["output_plan"]).encode())
    final = Path(destination) if destination else library.root / "installations" / identity
    try:
        from .manifest import parse

        with lock(library.root / f"locks/installation-{identity}", shared=True):
            raw = read_file(final / "manifest.json", MAX_MANIFEST_BYTES)
            manifest = parse(raw)
            plan = manifest.select(record["output_plan"], None)
            library._verify_directory(
                final,
                manifest,
                {
                    "snapshot": {"manifest_hash": record["output_manifest_hash"]},
                    "files": manifest.closure(plan),
                },
            )
            if destination:
                return {
                    "operation_id": record["operation_id"],
                    "state": "completed",
                    "output": destination,
                    "execution_validation": "unverified",
                }
            if not library.db.execute(
                "SELECT 1 FROM installations WHERE id=?", (identity,)
            ).fetchone():
                return None
            for operation in library.db.execute(
                "SELECT id,plan FROM operations WHERE state='completed'"
            ):
                saved = json.loads(operation["plan"])
                if saved["installation"] == identity:
                    return library.result(operation["id"], saved)
    except (OSError, RkaiError):
        return None
    return None


def recover(library):
    """Recover only stopped conversions; never restart SDK compilation at startup."""
    root = library.root / "conversions"
    if not root.exists():
        return
    for workspace in root.iterdir():
        from .storage import IDENTIFIER

        if not IDENTIFIER.fullmatch(workspace.name):
            continue
        try:
            with lock(library.root / ("locks/conversion-" + workspace.name)):
                path = workspace / "record.json"
                record = json.loads(read_file(path, MAX_MANIFEST_BYTES))
                if record["state"] not in {"completed", "failed", "cancelled"}:
                    completed = committed_output(library, record) is not None
                    record.update(
                        state="completed" if completed else "failed",
                        error=None if completed else "converter_owner_exited",
                    )
                    atomic_write(path, json.dumps(record).encode())
                alias = "conversion-input/" + workspace.name
                if library.db.execute("SELECT 1 FROM aliases WHERE name=?", (alias,)).fetchone():
                    library.delete(alias)
        except RkaiError as e:
            if e.code != "busy":
                raise
        except (OSError, ValueError, KeyError) as e:
            raise RkaiError(
                "corrupt_conversion_record", "Conversion recovery record is invalid"
            ) from e
