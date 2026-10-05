"""Regression evidence for M2 review fixes; native SDK/NPU execution remains separate."""

import copy
import hashlib
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
import zlib

import numpy as np
import onnx
import pytest
from onnx import TensorProto, helper
from pydantic import ValidationError
from test_execution import executable as executable
from test_execution import install

from rkai.adapters import configuration as adapter_configuration
from rkai.adapters import image_tensor, image_workspace_bytes
from rkai.conversion import Recipe, calibration_dataset, convert, recover
from rkai.environments import BUILD_ENVIRONMENTS, _command, recipe_lock
from rkai.errors import RkaiError
from rkai.execution import Session
from rkai.processes import group_alive, stop_group
from rkai.run_cli import consume
from rkai.storage import Library


def recipe_for(document, npu=False):
    plan = copy.deepcopy(document["plans"][0])
    if npu:
        plan["target"] = {
            "os": "linux",
            "arch": "aarch64",
            "accelerator": "rockchip-npu",
            "soc": "rk3588",
        }
        plan["components"][0].update(
            backend={"id": "rknn", "api": 1}, compatibility="rknn/2.3.2/rk3588"
        )
        plan["components"][0]["config"]["input_names"] = ["image"]
    return {
        "schema": "rkai.recipe/v0",
        "id": "review-regression",
        "model": {"id": "review/converted", "revision": "1"},
        "converter": "onnx-rknn" if npu else "onnx-import",
        "inputs": copy.deepcopy(document["files"]),
        "output_plan": plan,
    }


def declare(path, relative=None):
    raw = path.read_bytes()
    return {
        "path": relative or path.name,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


@pytest.mark.parametrize("interruption", ["signal", "exception"])
def test_sigint_after_install_commit_finishes_conversion(
    config, executable, tmp_path, monkeypatch, interruption
):
    root, _, document = executable
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe_for(document)))
    with Library(config) as library:
        original = library.acquire_manifest

        def interrupted(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1]["kind"] == "conversion":
                if interruption == "exception":
                    raise KeyboardInterrupt
                os.kill(os.getpid(), signal.SIGINT)
            return result

        monkeypatch.setattr(library, "acquire_manifest", interrupted)
        result = convert(library, str(root), str(path), alias="converted")
        assert result["state"] == "completed" and result["cancellation_deferred"]
        receipt = next((library.root / "conversions").glob("*/record.json"))
        assert json.loads(receipt.read_text())["state"] == "completed"
        recover(library)
        assert json.loads(receipt.read_text())["state"] == "completed"
        assert library.inspect("converted")["file_state"] == "ready"


def compressed_png(width, height):
    encoder = zlib.compressobj()
    row = b"\0" * (width * 3 + 1)
    pieces = [encoder.compress(row) for _ in range(height)]
    data = b"".join(pieces) + encoder.flush()

    def chunk(kind, data):
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", data)
        + chunk(b"IEND", b"")
    )


def test_compressed_image_rejected_before_decode_and_returns_input_status(
    config, executable, tmp_path, monkeypatch
):
    from PIL import PngImagePlugin

    path = tmp_path / "large.png"
    path.write_bytes(compressed_png(4000, 4000))
    assert path.stat().st_size < 100000

    def must_not_decode(*args):
        pytest.fail("Over-budget image decoded before rejection")

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", must_not_decode)
    with Library(config) as library:
        install(library, executable)
        with Session(library, "vision", allow_cpu=True) as session:
            # Parent patch does not affect worker; prove pre-load ordering directly too.
            cfg = adapter_configuration(session.plan.adapter)
            with pytest.raises(RkaiError) as error:
                image_tensor(path, cfg)
            assert error.value.code == "input_too_large"
            assert consume(session, {"image": str(path)}, True) == 2
            assert session.closed


def test_image_workspace_reserved_by_admission(config, executable):
    with Library(config) as library:
        install(library, executable)
        with Session(library, "vision", allow_cpu=True) as session:
            native = session.plan.components[0].config["memory_bytes"]
            assert session.memory_bytes == native + image_workspace_bytes(session.adapter_config)
        config.memory_budget_mb = 128
        with pytest.raises(RkaiError) as error:
            Session(library, "vision", allow_cpu=True)
        assert error.value.code == "resource_admission"


def test_image_calibration_matches_run_preprocessing(executable, tmp_path):
    root, image, document = executable
    data = recipe_for(document, npu=True)
    calibration = root / "calibration.png"
    calibration.write_bytes(image.read_bytes())
    data["inputs"]["calibration"] = declare(calibration)
    data["parameters"] = {"quantize": True, "calibration": [["calibration"]]}
    recipe = Recipe.model_validate(data)
    graph = onnx.load(root / "model.onnx")
    dataset = calibration_dataset(recipe, root, tmp_path / "compiler", graph)
    tensor = np.load(dataset.read_text().strip(), allow_pickle=False)
    cfg = adapter_configuration(recipe.output_plan.adapter)
    np.testing.assert_array_equal(tensor, image_tensor(image, cfg))
    np.testing.assert_array_equal(tensor[:, 0], np.ones((1, 2, 2)))
    np.testing.assert_array_equal(tensor[:, 1:], np.zeros((1, 2, 2, 2)))
    assert tensor.dtype == np.float32


def embedding_recipe(executable):
    root, _, document = executable
    data = recipe_for(document, npu=True)
    data["inputs"]["tokenizer"] = data["inputs"].pop("labels")
    names = ["input_ids", "attention_mask", "token_type_ids"]
    data["output_plan"]["capabilities"] = [{"id": "embedding", "version": 1}]
    data["output_plan"]["default_capability"] = "embedding"
    data["output_plan"]["adapter"] = {
        "id": "sentence-embedding",
        "api": 1,
        "config": {"tokenizer": "file:tokenizer", "input_names": names},
    }
    data["output_plan"]["components"][0]["config"]["input_names"] = names
    rows = []
    for sample in range(2):
        row = []
        for column in range(3):
            name = f"s{sample}_i{column}"
            path = root / (name + ".npy")
            np.save(path, np.full((1, 3), sample + column, dtype=np.int64))
            data["inputs"][name] = declare(path)
            row.append(name)
        rows.append(row)
    data["parameters"] = {"quantize": True, "calibration": rows}
    graph = helper.make_model(
        helper.make_graph(
            [],
            "calibration-metadata",
            [helper.make_tensor_value_info(name, TensorProto.INT64, [1, 3]) for name in names],
            [],
        )
    )
    return root, data, graph


def test_multi_input_calibration_groups_samples_and_preserves_order(executable, tmp_path):
    root, data, graph = embedding_recipe(executable)
    recipe = Recipe.model_validate(data)
    dataset = calibration_dataset(recipe, root, tmp_path / "compiler", graph)
    rows = [line.split() for line in dataset.read_text().splitlines()]
    assert len(rows) == 2 and all(len(row) == 3 for row in rows)
    for sample, row in enumerate(rows):
        for column, path in enumerate(row):
            np.testing.assert_array_equal(np.load(path), np.full((1, 3), sample + column))


@pytest.mark.parametrize("fault", ["precision", "columns", "order", "raw_text"])
def test_quantization_contract_rejects_unsupported_or_ambiguous_inputs(executable, fault):
    _, data, _ = embedding_recipe(executable)
    if fault == "precision":
        data["parameters"]["quantized_dtype"] = "W4A16"
    elif fault == "columns":
        data["parameters"]["calibration"][0].pop()
    elif fault == "order":
        data["parameters"].update(
            input_names=["token_type_ids", "attention_mask", "input_ids"], input_shapes=[[1, 3]] * 3
        )
    else:
        data["parameters"]["calibration"][0][0] = "tokenizer"
    with pytest.raises(ValidationError):
        Recipe.model_validate(data)


def test_calibration_npy_header_cannot_force_large_allocation(executable, tmp_path, monkeypatch):
    root, data, graph = embedding_recipe(executable)
    path = root / data["inputs"]["s0_i0"]["path"]
    with path.open("wb") as stream:
        np.lib.format.write_array_header_1_0(
            stream, {"descr": "<i8", "fortran_order": False, "shape": (20000000,)}
        )
    data["inputs"]["s0_i0"] = declare(path)
    recipe = Recipe.model_validate(data)
    monkeypatch.setattr(
        np, "load", lambda *_args, **_kwargs: pytest.fail("Allocated unsafe NPY header")
    )
    with pytest.raises(RkaiError) as error:
        calibration_dataset(recipe, root, tmp_path / "compiler", graph)
    assert error.value.code == "invalid_recipe"


def test_managed_command_diagnostics_drain_noise_without_leaking_input(caplog):
    code = (
        "import sys;print('secret-prompt https://host/?token=private'*30000);"
        "print(\"No module named 'torch'\",file=sys.stderr);sys.exit(1)"
    )
    with pytest.raises(RkaiError) as error:
        _command([sys.executable, "-c", code], stage="dependency-sync")
    details = error.value.details
    assert details["stage"] == "dependency-sync"
    assert "missing_module:torch" in details["diagnostics"]
    assert "secret-prompt" not in json.dumps(details) + caplog.text
    assert "token=private" not in json.dumps(details) + caplog.text


def test_rkllm_build_mode_is_scoped_and_part_of_identity(monkeypatch):
    monkeypatch.setattr("rkai.environments.host", lambda: ("linux", "x86_64"))
    before = recipe_lock("rkllm-convert")[2]
    cpu = recipe_lock("onnx-cpu")[2]
    assert BUILD_ENVIRONMENTS["rkllm-convert"] == {"BUILD_CUDA_EXT": "0", "UV_NO_CACHE": "true"}
    monkeypatch.setitem(BUILD_ENVIRONMENTS, "rkllm-convert", {"BUILD_CUDA_EXT": "1"})
    assert recipe_lock("rkllm-convert")[2] != before
    assert recipe_lock("onnx-cpu")[2] == cpu


def compile_hanging_library(root):
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("Native constructor lifecycle test requires a C compiler")
    library = root / "hang.so"
    code = """
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
__attribute__((constructor)) static void init(void) {
    FILE* f = fopen(getenv("RKAI_TEST_PID"), "w");
    fprintf(f, "%d", getpid());
    fclose(f);
    for (;;) sleep(1);
}
"""
    flags = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
    subprocess.run(
        [compiler, *flags, "-x", "c", "-", "-o", str(library)],
        input=code.encode(),
        check=True,
        capture_output=True,
    )
    return library


def test_dynamic_load_parent_sigkill_reaps_native_constructor(tmp_path):
    root = tmp_path.resolve()
    library = compile_hanging_library(root)
    pidfile = root / "native.pid"
    code = (
        "from pathlib import Path; from rkai.runtime_check import check_library;"
        "import sys; check_library(Path(sys.argv[1]))"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(library)],
        env={**os.environ, "RKAI_TEST_PID": str(pidfile)},
    )
    native = None
    try:
        deadline = time.monotonic() + 10
        while not pidfile.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists()
        native = int(pidfile.read_text())
        parent.kill()
        parent.wait()
        deadline = time.monotonic() + 5
        while group_alive(native) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not group_alive(native)
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait()
        if native:
            stop_group(native)


def test_dynamic_load_timeout_reaps_native_constructor(tmp_path, monkeypatch):
    from rkai.runtime_check import check_library

    root = tmp_path.resolve()
    library = compile_hanging_library(root)
    pidfile = root / "native.pid"
    monkeypatch.setenv("RKAI_TEST_PID", str(pidfile))
    monkeypatch.setattr("rkai.runtime_check.LOAD_TIMEOUT_SECONDS", 0.5)
    result = check_library(library)
    assert result["status"] == "fail" and "timed out" in result["evidence"]
    assert not group_alive(int(pidfile.read_text()))


@pytest.mark.parametrize("terminal,expected", [("completed", 0), ("failed", 2), ("cancelled", 130)])
def test_late_cli_interrupt_preserves_terminal_status(monkeypatch, terminal, expected):
    import rkai.run_cli as run_cli

    class Session:
        id = "session"

        def infer(self, data):
            yield {
                "type": terminal,
                "request_id": "request",
                "sequence": 3,
                "error": {"code": "invalid_input", "exit_code": 2},
            }

        def cancel(self):
            raise AssertionError("terminal request must not be cancelled again")

        def close(self):
            pass

    def interrupt(event, json_output):
        raise KeyboardInterrupt

    monkeypatch.setattr(run_cli, "emit", interrupt)
    assert run_cli.consume(Session(), {}, True) == expected
