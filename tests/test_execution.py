"""Real ONNX contexts and actual subprocess/socketpair lifecycle, not NPU evidence."""

import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import onnx
import pytest
from onnx import TensorProto, helper, numpy_helper
from PIL import Image
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

from rkai.capabilities import validate_request
from rkai.cli import main
from rkai.conversion import convert, recover
from rkai.environments import host
from rkai.errors import RkaiError
from rkai.execution import EventBuffer, Session
from rkai.ipc import MAX_FRAME_BYTES, encode, receive, send
from rkai.storage import Library, lock


def save_model(path, nodes, inputs, outputs, initializers=()):
    graph = helper.make_graph(
        nodes, "independent-test-model", inputs, outputs, initializer=list(initializers)
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    onnx.save(model, path)


@pytest.fixture
def executable(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    save_model(
        root / "model.onnx",
        [helper.make_node("ReduceMean", ["image"], ["scores"], axes=[2, 3], keepdims=0)],
        [helper.make_tensor_value_info("image", TensorProto.FLOAT, [1, 3, 2, 2])],
        [helper.make_tensor_value_info("scores", TensorProto.FLOAT, [1, 3])],
    )
    (root / "labels.txt").write_text("red\ngreen\nblue\n")
    image = tmp_path / "red.png"
    Image.new("RGB", (4, 4), (255, 0, 0)).save(image)
    files = {}
    for name, filename in [("model", "model.onnx"), ("labels", "labels.txt")]:
        raw = (root / filename).read_bytes()
        files[name] = {
            "path": filename,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    os_name, arch = host()
    plan = {
        "id": "cpu",
        "target": {"os": os_name, "arch": arch, "accelerator": "cpu"},
        "capabilities": [{"id": "classification", "version": 1}],
        "default_capability": "classification",
        "adapter": {
            "id": "image-classification",
            "api": 1,
            "config": {
                "labels": "file:labels",
                "input_name": "image",
                "width": 2,
                "height": 2,
                "mean": [0.0, 0.0, 0.0],
                "std": [1.0, 1.0, 1.0],
            },
        },
        "components": [
            {
                "id": "model",
                "backend": {"id": "onnxruntime", "api": 1},
                "compatibility": "onnxruntime/1.23.2/cpu",
                "files": {"model": "model"},
                "config": {
                    "memory_bytes": 128 * 1024 * 1024,
                    "memory_evidence": "test allowance; not a board estimate",
                },
            }
        ],
    }
    document = {
        "schema": "rkai.model/v0",
        "model": {"id": "test/vision", "revision": "1"},
        "sources": {},
        "files": files,
        "plans": [plan],
    }
    (root / "manifest.json").write_text(json.dumps(document))
    # Test installation admission uses the dev environment; separate smoke validates managed prep.
    monkeypatch.setattr("rkai.execution.resolve", lambda *_: Path(sys.prefix))
    monkeypatch.setattr("rkai.conversion.resolve", lambda *_: Path(sys.prefix))
    monkeypatch.setattr("rkai.conversion.prepare", lambda *_: {})
    return root, image, document


def install(library, executable):
    return library.pull(str(executable[0] / "manifest.json"), plan_id="cpu", alias="vision")


def test_real_classification_lease_and_repeated_requests(config, executable):
    with Library(config) as library:
        install(library, executable)
        with Session(library, "vision", allow_cpu=True) as session:
            with pytest.raises(RkaiError, match="owned"):
                library.delete("vision")
            with pytest.raises(RkaiError) as busy:
                Session(library, "vision", allow_cpu=True)
            assert busy.value.code == "busy"
            for _ in range(2):
                events = list(session.infer({"image": str(executable[1]), "top_k": 2}))
                assert [e["type"] for e in events] == ["accepted", "started", "output", "completed"]
                labels = events[2]["payload"]["labels"]
                assert labels[0]["label"] == "red"
                assert labels[0]["score"] == pytest.approx(np.exp(1) / (np.exp(1) + 2))
                assert [e["sequence"] for e in events] == list(range(4))
            process = session.process
        assert process.poll() is not None
        assert library.inspect("vision")["execution_validation"] == "unverified"
        evidence = json.loads(
            library.db.execute("SELECT evidence FROM execution_evidence").fetchone()[0]
        )
        assert evidence["device"] == "cpu"
        library.delete("vision")


def test_cpu_opt_in_corruption_and_memory(config, executable):
    with Library(config) as library:
        result = install(library, executable)
        with pytest.raises(RkaiError) as disabled:
            Session(library, "vision")
        assert disabled.value.code == "cpu_fallback_disabled"
        config.memory_budget_mb = 128
        document = executable[2]
        document["model"]["revision"] = "2"
        document["plans"][0]["components"][0]["config"]["memory_bytes"] = 1024 * 1024 * 1024
        (executable[0] / "manifest.json").write_text(json.dumps(document))
        library.pull(str(executable[0] / "manifest.json"), plan_id="cpu", alias="large")
        with pytest.raises(RkaiError) as admission:
            Session(library, "large", allow_cpu=True)
        assert admission.value.code == "resource_admission"
        (library.root / "installations" / result["installation"] / "model.onnx").write_bytes(b"bad")
        with pytest.raises(RkaiError) as corrupt:
            Session(library, "vision", allow_cpu=True)
        assert corrupt.value.code == "corrupt_installation"


def test_worker_crash_and_generator_abandonment(config, executable):
    with Library(config) as library:
        install(library, executable)
        session = Session(library, "vision", allow_cpu=True)
        stream = session.infer({"image": str(executable[1])})
        assert next(stream)["type"] == "accepted"
        stream.close()
        assert session.closed and session.process.poll() is not None
        with Session(library, "vision", allow_cpu=True) as session:
            os.killpg(session.process.pid, signal.SIGKILL)
            session.process.wait()
            with pytest.raises((BrokenPipeError, RkaiError)):
                list(session.infer({"image": str(executable[1])}))
        library.delete("vision")


def test_backend_failure_retires_context(config, executable):
    bad = executable[1].with_name("bad-image")
    bad.write_bytes(b"not an image")
    with Library(config) as library:
        install(library, executable)
        with Session(library, "vision", allow_cpu=True) as session:
            events = list(session.infer({"image": str(bad)}))
            assert events[-1]["type"] == "failed"
            assert session.process.poll() is not None
            assert session.closed
        library.delete("vision")


def test_real_embedding_batch_semantics(config, executable):
    root, _, document = executable
    tokenizer = Tokenizer(
        WordLevel({"[UNK]": 0, "red": 1, "blue": 2, "green": 3}, unk_token="[UNK]")
    )
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(root / "tokenizer.json"))
    weights = np.array([[0, 0, 1], [1, 0, 0], [0, 1, 0], [1, 1, 0]], dtype=np.float32)
    save_model(
        root / "embedding.onnx",
        [helper.make_node("Gather", ["weights", "input_ids"], ["hidden"])],
        [
            helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "seq"]),
            helper.make_tensor_value_info("attention_mask", TensorProto.INT64, ["batch", "seq"]),
        ],
        [helper.make_tensor_value_info("hidden", TensorProto.FLOAT, ["batch", "seq", 3])],
        [numpy_helper.from_array(weights, "weights")],
    )
    files = {}
    for name, filename in [("model", "embedding.onnx"), ("tokenizer", "tokenizer.json")]:
        raw = (root / filename).read_bytes()
        files[name] = {
            "path": filename,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    plan = document["plans"][0]
    plan["capabilities"] = [{"id": "embedding", "version": 1}]
    plan["default_capability"] = "embedding"
    plan["adapter"] = {
        "id": "sentence-embedding",
        "api": 1,
        "config": {
            "tokenizer": "file:tokenizer",
            "input_names": ["input_ids", "attention_mask"],
            "max_length": 4,
        },
    }
    document["model"] = {"id": "test/embedding", "revision": "1"}
    document["files"] = files
    (root / "manifest.json").write_text(json.dumps(document))
    with Library(config) as library:
        install(library, executable)
        with Session(library, "vision", allow_cpu=True) as session:
            events = list(session.infer({"texts": ["red", "red blue"]}))
            result = events[2]["payload"]
            assert result["vectors"][0] == [1.0, 0.0, 0.0]
            assert result["vectors"][1] == pytest.approx([2**-0.5, 2**-0.5, 0])
            assert events[-1]["type"] == "completed"
            failed = list(session.infer({"texts": ["red " * 10]}))
            assert failed[-1]["error"]["code"] == "context_limit"


def test_real_convert_install_and_export(config, executable, tmp_path):
    root, _, document = executable
    recipe = {
        "schema": "rkai.recipe/v0",
        "id": "independent-onnx-import",
        "model": {"id": "test/converted", "revision": "1"},
        "converter": "onnx-import",
        "inputs": document["files"],
        "output_plan": document["plans"][0],
    }
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe))
    with Library(config) as library:
        result = convert(library, str(root / "model.onnx"), str(path), alias="converted")
        assert result["execution_validation"] == "unverified"
        with Session(library, "converted", allow_cpu=True) as session:
            assert list(session.infer({"image": str(executable[1])}))[-1]["type"] == "completed"
        assert not library.db.execute(
            "SELECT 1 FROM aliases WHERE name LIKE 'conversion-input/%'"
        ).fetchone()
        output = tmp_path / "export"
        convert(library, str(root), str(path), output=str(output))
        record = json.loads((output / "conversion-record.json").read_text())
        assert record["conversion_evidence"]["cpu_load"] == "passed"
        assert str(root) not in json.dumps(record)
        with pytest.raises(RkaiError) as exists:
            convert(library, str(root), str(path), output=str(output))
        assert exists.value.code == "output_exists"
        imported = library.pull(str(output / "manifest.json"), plan_id="cpu", alias="exported")
        assert imported["file_state"] == "ready"
        library.delete("exported")
        assert (output / "model.onnx").exists()


def test_conversion_crash_recovery_uses_lock(config, tmp_path):
    with Library(config) as library:
        operation = "a" * 32
        workspace = library.root / "conversions" / operation
        workspace.mkdir(parents=True)
        path = workspace / "record.json"
        path.write_text(json.dumps({"operation_id": operation, "state": "converting"}))
        with lock(library.root / ("locks/conversion-" + operation)):
            recover(library)
            assert json.loads(path.read_text())["state"] == "converting"
        recover(library)
        assert json.loads(path.read_text())["state"] == "failed"


def test_framing_fragmented_and_hostile():
    left, right = socket.socketpair()
    try:
        raw = encode({"type": "hello", "version": 1})
        writer = threading.Thread(
            target=lambda: [left.sendall(raw[i : i + 1]) for i in range(len(raw))]
        )
        writer.start()
        assert receive(right) == {"type": "hello", "version": 1}
        writer.join()
        left.sendall(struct.pack("!I", MAX_FRAME_BYTES + 1))
        with pytest.raises(RkaiError, match="length"):
            receive(right)
        send(left, {"ok": True})
        assert receive(right) == {"ok": True}
        left.sendall(struct.pack("!I", 10) + b"{")
        with pytest.raises(RkaiError) as timeout:
            receive(right, 0.05)
        assert timeout.value.code == "ipc_timeout"
    finally:
        left.close()
        right.close()


def test_bounded_buffer_backpressure(monkeypatch):
    import rkai.execution as execution

    monkeypatch.setattr(execution, "QUEUE_EVENTS", 1)
    monkeypatch.setattr(execution, "IO_TIMEOUT_SECONDS", 0.02)
    buffer = EventBuffer()
    buffer.put({"type": "output"})
    with pytest.raises(RkaiError) as slow:
        buffer.put({"type": "output"})
    assert slow.value.code == "backpressure"
    assert buffer.get()["type"] == "output"
    monkeypatch.setattr(execution, "QUEUE_BYTES", 30)
    with pytest.raises(RkaiError):
        buffer.put({"text": "x" * 30})


def test_typed_input_rejected_before_acceptance(config, executable):
    with Library(config) as library:
        install(library, executable)
        with Session(library, "vision", allow_cpu=True) as session:
            with pytest.raises(RkaiError) as invalid:
                list(session.infer({"image": str(executable[1]), "top_k": True}))
            assert invalid.value.code == "invalid_input"
            assert session.active is None
    with pytest.raises(RkaiError):
        validate_request("decision", {"context": "not implemented"})


def test_cli_real_run_no_download(config, executable, capsys):
    with Library(config) as library:
        install(library, executable)
    args = [
        "--data-dir",
        config.data_dir,
        "run",
        "-m",
        "vision",
        "--allow-cpu-fallback",
        "--input",
        json.dumps({"image": str(executable[1])}),
        "--json",
    ]
    assert main(args) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["type"] == "completed"
    assert (
        main(["--data-dir", config.data_dir, "run", "-m", "missing", "--input", "x", "--json"]) == 2
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "unknown_alias"
    import logging

    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)


def hanging_model(executable):
    root, _, document = executable
    body = helper.make_graph(
        [
            helper.make_node("Identity", ["condition"], ["continue"]),
            helper.make_node("Identity", ["value"], ["value_out"]),
        ],
        "infinite-body",
        [
            helper.make_tensor_value_info("iteration", TensorProto.INT64, []),
            helper.make_tensor_value_info("condition", TensorProto.BOOL, []),
            helper.make_tensor_value_info("value", TensorProto.FLOAT, [1, 3]),
        ],
        [
            helper.make_tensor_value_info("continue", TensorProto.BOOL, []),
            helper.make_tensor_value_info("value_out", TensorProto.FLOAT, [1, 3]),
        ],
    )
    save_model(
        root / "model.onnx",
        [
            helper.make_node("ReduceMean", ["image"], ["initial"], axes=[2, 3], keepdims=0),
            helper.make_node("Loop", ["", "always", "initial"], ["scores"], body=body),
        ],
        [helper.make_tensor_value_info("image", TensorProto.FLOAT, [1, 3, 2, 2])],
        [helper.make_tensor_value_info("scores", TensorProto.FLOAT, [1, 3])],
        [numpy_helper.from_array(np.array(True), "always")],
    )
    raw = (root / "model.onnx").read_bytes()
    document["files"]["model"].update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    document["model"]["revision"] = "infinite-loop"
    (root / "manifest.json").write_text(json.dumps(document))


def test_native_hang_cancellation_reaped_before_terminal(config, executable, monkeypatch):
    import rkai.execution as execution
    from rkai.processes import group_alive

    hanging_model(executable)
    monkeypatch.setattr(execution, "CANCEL_GRACE_SECONDS", 0.1)
    with Library(config) as library:
        install(library, executable)
        session = Session(library, "vision", allow_cpu=True)
        stream = session.infer({"image": str(executable[1])})
        assert next(stream)["type"] == "accepted"
        assert next(stream)["type"] == "started"
        with pytest.raises(RkaiError) as used:
            library.delete("vision")
        assert used.value.code == "busy"
        session.cancel()
        session.cancel()
        terminal = list(stream)[-1]
        assert terminal["type"] == "cancelled"
        assert session.closed and session.process.poll() is not None
        assert not group_alive(session.process.pid)
        library.delete("vision")


def test_native_hang_request_deadline(config, executable, monkeypatch):
    import rkai.execution as execution
    from rkai.processes import group_alive

    hanging_model(executable)
    monkeypatch.setattr(execution, "REQUEST_TIMEOUT_SECONDS", 0.1)
    with Library(config) as library:
        install(library, executable)
        with Session(library, "vision", allow_cpu=True) as session:
            events = list(session.infer({"image": str(executable[1])}))
            assert events[-1]["type"] == "failed"
            assert events[-1]["error"]["code"] == "ipc_timeout"
            assert not group_alive(session.process.pid)
        library.delete("vision")


def test_parent_sigkill_stops_native_worker_and_releases_lease(config, executable, tmp_path):
    from rkai.processes import group_alive

    hanging_model(executable)
    with Library(config) as library:
        install(library, executable)
    pid_file = tmp_path / "group.json"
    code = """
import json,sys,time
from pathlib import Path
from rkai.config import Config
from rkai.storage import Library
import rkai.execution as execution
execution.resolve=lambda *_: Path(sys.prefix)
with Library(Config(data_dir=sys.argv[1])) as library:
 with execution.Session(library,'vision',allow_cpu=True) as session:
  stream=session.infer({'image':sys.argv[3]})
  assert next(stream)['type']=='accepted'
  assert next(stream)['type']=='started'
  Path(sys.argv[2]).write_text(json.dumps({'group':session.process.pid,'native':session.process.native_group}))
  time.sleep(60)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", code, config.data_dir, str(pid_file), str(executable[1])]
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        saved = json.loads(pid_file.read_text())
        group, native = saved["group"], saved["native"]
        assert group_alive(group) and group_alive(native)
        # Pause cleanup to prove locks survive the application owner's death.
        os.kill(group, signal.SIGSTOP)
        parent.kill()
        parent.wait()
        with Library(config) as library:
            with pytest.raises(RkaiError) as retained:
                library.delete("vision")
            assert retained.value.code == "busy"
        os.kill(group, signal.SIGCONT)
        deadline = time.monotonic() + 5
        while (group_alive(group) or group_alive(native)) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not group_alive(group) and not group_alive(native)
        with Library(config) as library:
            library.delete("vision")
    finally:
        if pid_file.exists():
            try:
                os.kill(json.loads(pid_file.read_text())["group"], signal.SIGCONT)
            except ProcessLookupError:
                pass
        if parent.poll() is None:
            parent.kill()
        parent.wait()


def test_doctor_real_cpu_probe_is_not_npu_evidence(config, executable):
    from rkai.doctor import doctor

    root, image, document = executable
    raw = image.read_bytes()
    (root / "probe.png").write_bytes(raw)
    document["files"]["probe"] = {
        "path": "probe.png",
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    document["plans"][0]["adapter"]["config"]["probe"] = {"image": "file:probe", "top_k": 1}
    (root / "manifest.json").write_text(json.dumps(document))
    config.allow_cpu_fallback = True
    with Library(config) as library:
        install(library, executable)
    result = doctor(config, "vision", probe=True)
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["probe"]["status"] == "pass"
    assert result["active_probe"] == "cpu-passed"
    assert checks["compatibility"]["status"] == "unknown"
    with Library(config) as library:
        assert library.inspect("vision")["execution_validation"] == "unverified"


def test_cpu_fallback_requires_installed_independent_artifact(config, executable, monkeypatch):
    import copy

    import rkai.execution as execution

    root, image, document = executable
    # Selection fixture is deliberately not an executable RKNN graph.
    (root / "npu.rknn").write_bytes(b"selection-only-not-npu-evidence")
    raw = (root / "npu.rknn").read_bytes()
    document["files"]["npu"] = {
        "path": "npu.rknn",
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    plan = copy.deepcopy(document["plans"][0])
    plan["id"] = "npu"
    plan["target"] = {
        "os": "linux",
        "arch": "aarch64",
        "accelerator": "rockchip-npu",
        "soc": "rk3588",
    }
    component = plan["components"][0]
    component.update(
        backend={"id": "rknn", "api": 1}, compatibility="rknn/2.3.2/rk3588", files={"model": "npu"}
    )
    document["plans"].append(plan)
    (root / "manifest.json").write_text(json.dumps(document))
    original = execution.admit

    def admit(library, plan, capability, allow_cpu):
        if plan.id == "npu":
            raise RkaiError("unsupported_host", "test unavailable target", 3)
        return original(library, plan, capability, allow_cpu)

    monkeypatch.setattr(execution, "admit", admit)
    with Library(config) as library:
        library.pull(str(root / "manifest.json"), plan_id="npu", alias="npu")
        with pytest.raises(RkaiError) as missing:
            Session(library, "npu", allow_cpu=True)
        assert missing.value.code == "cpu_artifact_missing"
        library.pull(str(root / "manifest.json"), plan_id="cpu", alias="cpu")
        with Session(library, "npu", allow_cpu=True) as session:
            assert session.plan.id == "cpu"
            assert session.selection_reason.endswith("unsupported_host")
            assert list(session.infer({"image": str(image)}))[-1]["type"] == "completed"
            with pytest.raises(RkaiError) as leased:
                library.delete("npu")
            assert leased.value.code == "busy"


def test_fragmented_socket_write_has_deadline(monkeypatch):
    import rkai.ipc as ipc

    monkeypatch.setattr(ipc, "IO_TIMEOUT_SECONDS", 0.05)
    left, right = socket.socketpair()
    try:
        left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        with pytest.raises(RkaiError) as blocked:
            send(left, {"payload": "x" * 1024 * 1024})
        assert blocked.value.code == "ipc_timeout"
    finally:
        left.close()
        right.close()


def test_v1_library_migration_preserves_installed_data(config, executable):
    with Library(config) as library:
        installed = install(library, executable)
        library.db.execute("DROP TABLE execution_evidence")
        library.db.execute("PRAGMA user_version=1")
    with Library(config) as library:
        assert library.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            library.db.execute("SELECT installation FROM aliases WHERE name='vision'").fetchone()[0]
            == installed["installation"]
        )
        assert library.db.execute("SELECT count(*) FROM execution_evidence").fetchone()[0] == 0


def test_failed_conversion_preserves_previous_alias_and_cleans_input_lease(
    config, executable, tmp_path
):
    root, _, document = executable
    recipe = {
        "schema": "rkai.recipe/v0",
        "id": "invalid-graph",
        "model": {"id": "conversion/broken", "revision": "1"},
        "converter": "onnx-import",
        "inputs": document["files"].copy(),
        "output_plan": document["plans"][0],
    }
    with Library(config) as library:
        before = install(library, executable)
        (root / "model.onnx").write_bytes(b"invalid ONNX graph")
        raw = (root / "model.onnx").read_bytes()
        recipe["inputs"]["model"] = {
            "path": "model.onnx",
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        path = tmp_path / "broken.json"
        path.write_text(json.dumps(recipe))
        with pytest.raises(RkaiError) as failed:
            convert(library, str(root), str(path), alias="vision")
        assert failed.value.code == "conversion_failed"
        assert (
            library.db.execute("SELECT installation FROM aliases WHERE name='vision'").fetchone()[0]
            == before["installation"]
        )
        assert not library.db.execute(
            "SELECT 1 FROM aliases WHERE name LIKE 'conversion-input/%'"
        ).fetchone()
        records = list((library.root / "conversions").glob("*/record.json"))
        assert json.loads(records[0].read_text())["state"] == "failed"
        library.delete("vision")


def test_unsupported_conversion_host_rejects_before_network(
    config, executable, monkeypatch, tmp_path
):
    import copy

    import rkai.environments as environments

    document = executable[2]
    plan = copy.deepcopy(document["plans"][0])
    plan["target"] = {
        "os": "linux",
        "arch": "aarch64",
        "accelerator": "rockchip-npu",
        "soc": "rk3588",
    }
    plan["components"][0].update(
        backend={"id": "rknn", "api": 1}, compatibility="rknn/2.3.2/rk3588"
    )
    recipe = {
        "schema": "rkai.recipe/v0",
        "id": "unsupported-host",
        "model": document["model"],
        "converter": "onnx-rknn",
        "inputs": document["files"],
        "output_plan": plan,
    }
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe))
    monkeypatch.setattr(environments, "host", lambda: ("darwin", "aarch64"))
    with Library(config) as library:
        with pytest.raises(RkaiError) as blocked:
            convert(library, "hf://unreachable/model@main", str(path))
        assert blocked.value.code == "unsupported_conversion_host"
        assert not library.db.execute("SELECT 1 FROM operations").fetchone()


def test_guardian_crash_stops_hanging_native_child(config, executable):
    from rkai.processes import group_alive

    hanging_model(executable)
    with Library(config) as library:
        install(library, executable)
        with Session(library, "vision", allow_cpu=True) as session:
            stream = session.infer({"image": str(executable[1])})
            assert next(stream)["type"] == "accepted"
            assert next(stream)["type"] == "started"
            os.kill(session.process.pid, signal.SIGKILL)
            terminal = list(stream)[-1]
            assert terminal["type"] == "failed"
            assert session.closed and not group_alive(session.process.pid)
            assert not group_alive(session.process.native_group)
        library.delete("vision")


def test_conversion_hf_head_resolved_once_for_all_inputs(config, executable, monkeypatch):
    from contextlib import nullcontext

    import rkai.conversion as conversion

    document = executable[2]
    recipe = conversion.Recipe.model_validate(
        {
            "schema": "rkai.recipe/v0",
            "id": "branch-race",
            "model": document["model"],
            "converter": "onnx-import",
            "inputs": document["files"],
            "output_plan": document["plans"][0],
        }
    )
    calls = []

    class AdvancingProvider:
        def __init__(self, *_):
            pass

        def fixed_file(self, repository, selector, path):
            calls.append(selector)
            # A second lookup of the mutable branch would resolve a different commit.
            return (
                ("a" if len(calls) == 1 else "b") * 40 if selector == "main" else selector
            ), path

    monkeypatch.setattr(conversion, "client", lambda: nullcontext(object()))
    monkeypatch.setattr(conversion, "Provider", AdvancingProvider)
    with Library(config) as library:
        sources, _, snapshot = conversion.source_manifest(
            library, "hf://public/model@main", recipe, None
        )
        assert calls == ["main", "a" * 40]
        assert {source["revision"] for source in sources.values()} == {"a" * 40}
        assert set(snapshot["file_revisions"].values()) == {"a" * 40}


@pytest.mark.parametrize("backend", ["rknn", "rkllm"])
def test_npu_backend_cannot_be_declared_as_cpu(config, executable, backend):
    root, _, document = executable
    plan = document["plans"][0]
    component = plan["components"][0]
    component["backend"]["id"] = backend
    component["compatibility"] = "rknn/2.3.2/rk3588" if backend == "rknn" else "rkllm/1.3.1/rk3588"
    if backend == "rkllm":
        plan["capabilities"] = [{"id": "chat", "version": 1}]
        plan["default_capability"] = "chat"
        plan["adapter"] = {
            "id": "prompt-chat",
            "api": 1,
            "config": {
                "tokenizer": "file:labels",
                "role_prefixes": {"system": "s", "user": "u", "assistant": "a"},
                "message_suffix": "\n",
                "assistant_prefix": "a",
            },
        }
    (root / "manifest.json").write_text(json.dumps(document))
    with Library(config) as library:
        install(library, executable)
        with pytest.raises(RkaiError) as invalid:
            Session(library, "vision", allow_cpu=True)
        assert invalid.value.code == "invalid_backend"
    from rkai.doctor import doctor

    checks = {c["name"]: c for c in doctor(config, "vision")["checks"]}
    assert checks["model_contract"]["status"] == "fail"


def test_fallback_filters_foreign_cpu_targets(config, executable, monkeypatch):
    import copy

    import rkai.execution as execution

    root, image, document = executable
    cpu = document["plans"][0]
    foreign = copy.deepcopy(cpu)
    foreign["id"] = "foreign-cpu"
    foreign["target"]["os"] = "linux" if cpu["target"]["os"] != "linux" else "darwin"
    npu = copy.deepcopy(cpu)
    npu["id"] = "npu"
    npu["target"] = {
        "os": "linux",
        "arch": "aarch64",
        "accelerator": "rockchip-npu",
        "soc": "rk3588",
    }
    npu["components"][0].update(backend={"id": "rknn", "api": 1}, compatibility="rknn/2.3.2/rk3588")
    document["plans"] += [foreign, npu]
    (root / "manifest.json").write_text(json.dumps(document))
    original = execution.admit

    def admit(library, plan, capability, allow_cpu):
        if plan.id == "npu":
            raise RkaiError("unsupported_host", "selection fixture", 3)
        return original(library, plan, capability, allow_cpu)

    monkeypatch.setattr(execution, "admit", admit)
    with Library(config) as library:
        for plan in document["plans"]:
            library.pull(str(root / "manifest.json"), plan_id=plan["id"], alias=plan["id"])
        with Session(library, "npu", allow_cpu=True) as session:
            assert session.plan.id == "cpu"
            assert list(session.infer({"image": str(image)}))[-1]["type"] == "completed"


@pytest.mark.parametrize("raw", [b"[]", b"null", b"[" * 2000])
def test_corrupt_environment_receipt_is_controlled(config, tmp_path, monkeypatch, raw):
    import rkai.environments as environments

    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "ready.json").write_bytes(raw)
    monkeypatch.setattr(environments, "location", lambda *_: environment)
    with Library(config) as library, pytest.raises(RkaiError) as failure:
        environments.resolve(library, "onnx-cpu")
    assert failure.value.code == "environment_unprepared"
