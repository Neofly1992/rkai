"""Versioned, isolated SDK environments; preparation is explicit network work."""

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .download import atomic_write, download, verify
from .errors import RkaiError
from .files import ensure_directory, read_file, remove_tree
from .manifest import File, digest
from .processes import handshake, launch_worker, stop
from .sources import client
from .storage import lock

PYTHON_VERSION = "3.12.13"
TOOLCHAINS = Path(__file__).resolve().with_name("toolchains")
ENV_KINDS = {
    "onnx-cpu",
    "llama-cpu",
    "rknn-convert",
    "rknn-runtime",
    "rkllm-runtime",
    "rkllm-convert",
}

# CPython 3.12 has no AutoGPTQ 0.7.1 wheel. Its official source supports
# BUILD_CUDA_EXT=0: compile the Python implementation without importing torch
# in an isolated build or requiring a host CUDA toolchain. This is converter
# dependency preparation, not a CPU substitute for RKLLM/NPU execution.
# uv's shared built-wheel cache does not encode this source package's custom
# build environment. Avoid reusing an ambient CUDA-enabled AutoGPTQ build.
BUILD_ENVIRONMENTS = {"rkllm-convert": {"BUILD_CUDA_EXT": "0", "UV_NO_CACHE": "true"}}


def host():
    arch = platform.machine().lower()
    return platform.system().lower(), {"arm64": "aarch64", "amd64": "x86_64"}.get(arch, arch)


def recipe_lock(kind):
    os_name, arch = host()
    if kind not in ENV_KINDS:
        raise RkaiError("unsupported_environment", "Unknown managed environment", 3)
    if kind.startswith(("rknn", "rkllm")) and (
        os_name != "linux" or arch not in ("aarch64", "x86_64")
    ):
        raise RkaiError(
            "unsupported_conversion_host",
            "Rockchip toolchains require Linux and Python 3.12 (RKNN: ARM64/x86_64; "
            "RKLLM converter: x86_64); "
            "convert on a Linux host with --output, then import on RK3588",
            3,
        )
    if kind.endswith("runtime") and arch != "aarch64":
        raise RkaiError(
            "unsupported_runtime_host", "Rockchip NPU runtimes require Linux aarch64", 3
        )
    if kind == "rkllm-convert" and arch != "x86_64":
        raise RkaiError(
            "unsupported_conversion_host", "RKLLM 1.3.1 conversion requires Linux x86_64", 3
        )
    name = (
        "onnx-cpu"
        if kind in ("rknn-runtime", "rkllm-runtime")
        else ("rknn-convert-" + arch if kind == "rknn-convert" else kind)
    )
    dependencies = read_file(TOOLCHAINS / (name + ".txt"))
    assets = json.loads(read_file(TOOLCHAINS / "rockchip-assets.json"))
    selected = []
    if kind == "rknn-convert":
        selected = [
            a
            for a in assets
            if "rknn_toolkit2-" in a["path"]
            and ("/arm64/" if arch == "aarch64" else "/x86_64/") in a["path"]
        ]
    elif kind == "rknn-runtime":
        selected = [a for a in assets if a["path"].endswith("librknnrt.so")]
    elif kind == "rkllm-runtime":
        selected = [a for a in assets if a["path"].endswith(("rkllm.h", "librkllmrt.so"))]
    elif kind == "rkllm-convert":
        selected = [a for a in assets if "rkllm_toolkit-" in a["path"]]
    if kind.startswith(("rknn", "rkllm")):
        repository = "rknn-toolkit2" if kind.startswith("rknn") else "rknn-llm"
        selected += [a for a in assets if a["path"] == repository + "/LICENSE"]
    bridge = (
        read_file(Path(__file__).resolve().with_name("native") / "rkllm_bridge.cpp")
        if kind == "rkllm-runtime"
        else b""
    )
    identity = digest(
        dependencies
        + read_file(TOOLCHAINS / "build-constraints.txt")
        + b"cpu-only-cmake-v1"
        + bridge
        + json.dumps(selected, sort_keys=True).encode()
        + (PYTHON_VERSION + ":" + os_name + ":" + arch).encode()
        + (
            json.dumps(BUILD_ENVIRONMENTS[kind], sort_keys=True).encode()
            if kind in BUILD_ENVIRONMENTS
            else b""
        )
    )
    return dependencies, selected, identity


def location(library, kind):
    _, _, identity = recipe_lock(kind)
    return library.root / "environments" / (kind + "-" + identity)


def resolve(library, kind):
    root = location(library, kind)
    dependencies, assets, identity = recipe_lock(kind)
    try:
        record = json.loads(read_file(root / "ready.json", 65536))
        if (
            not isinstance(record, dict)
            or record.get("lock") != identity
            or not (root / "bin/python").is_file()
        ):
            raise ValueError
        if kind == "rkllm-runtime" and hashlib.sha256(
            read_file(root / "assets/rkllm_bridge.so")
        ).hexdigest() != record.get("bridge_sha256"):
            raise ValueError
        for asset in assets:
            spec = File(
                path=asset.get("name", asset["path"].rsplit("/", 1)[-1]),
                size=asset["size"],
                sha256=asset["sha256"],
            )
            if not verify(root / "assets" / spec.path, spec):
                raise ValueError
    except (OSError, ValueError, RecursionError, RkaiError) as e:
        raise RkaiError(
            "environment_unprepared",
            f"Prepare the isolated environment with: rkai runtime prepare {kind}",
            3,
        ) from e
    return root


def prepare(library, kind):
    dependencies, assets, identity = recipe_lock(kind)
    root = location(library, kind)
    uv = shutil.which("uv")
    if not uv:
        raise RkaiError("missing_uv", "Install uv to prepare managed environments", 3)
    ensure_directory(root.parent)
    with lock(library.root / ("locks/environment-" + identity)) as environment_fd:
        if (root / "ready.json").exists():
            resolve(library, kind)
            return {"environment": kind, "lock": identity, "state": "ready"}
        if root.exists():
            remove_tree(root)
        ensure_directory(root)
        ensure_directory(root / "assets")
        try:
            requirements = root / "requirements.txt"
            atomic_write(requirements, dependencies)
            constraints = root / "build-constraints.txt"
            atomic_write(constraints, read_file(TOOLCHAINS / "build-constraints.txt"))
            commands = [
                [uv, "venv", "--python", PYTHON_VERSION, "--allow-existing", str(root)],
                [
                    uv,
                    "pip",
                    "sync",
                    "--python",
                    str(root / "bin/python"),
                    "--require-hashes",
                    "--build-constraints",
                    str(constraints),
                    str(requirements),
                ],
            ]
            for command in commands:
                _command(
                    command,
                    [environment_fd],
                    kind,
                    "python-venv" if command[1] == "venv" else "dependency-sync",
                )
            with client() as http:
                # Retrieve and retain license before any vendor binary.
                assets = sorted(assets, key=lambda a: not a["path"].endswith("/LICENSE"))
                for asset in assets:
                    name = asset.get("name", asset["path"].rsplit("/", 1)[-1])
                    spec = File(path=name, size=asset["size"], sha256=asset["sha256"])
                    download(http, [asset["url"]], root / "assets" / name, spec, root / "transfers")
                    if asset["path"].endswith("/LICENSE"):
                        logging.getLogger("rkai.environments").warning(
                            "SDK license: %s (retained at %s); "
                            "preparation/use subject to these terms",
                            asset["url"],
                            root / "assets" / name,
                        )
                    if name.endswith(".whl"):
                        _command(
                            [
                                uv,
                                "pip",
                                "install",
                                "--python",
                                str(root / "bin/python"),
                                "--no-deps",
                                str(root / "assets" / name),
                            ],
                            [environment_fd],
                            kind,
                            "vendor-wheel-install",
                        )
            if kind == "rkllm-runtime":
                compiler = shutil.which("c++")
                if not compiler:
                    raise RkaiError(
                        "missing_compiler",
                        "Install a C++ compiler to build the pinned RKLLM bridge",
                        3,
                    )
                _command(
                    [
                        compiler,
                        "-std=c++17",
                        "-shared",
                        "-fPIC",
                        "-O2",
                        "-I" + str(root / "assets"),
                        str(Path(__file__).resolve().with_name("native") / "rkllm_bridge.cpp"),
                        "-L" + str(root / "assets"),
                        "-lrkllmrt",
                        "-Wl,-rpath,$ORIGIN",
                        "-o",
                        str(root / "assets/rkllm_bridge.so"),
                    ],
                    [environment_fd],
                    kind,
                    "rkllm-bridge-build",
                )
            probes = {
                "onnx-cpu": "import onnx,onnxruntime,PIL,tokenizers",
                "llama-cpu": "import llama_cpp,onnx,onnxruntime,tokenizers",
                "rknn-convert": "from rknn.api import RKNN",
                "rkllm-convert": "from rkllm.api import RKLLM",
                "rknn-runtime": "import ctypes;ctypes.CDLL("
                + repr(str(root / "assets/librknnrt.so"))
                + ")",
                "rkllm-runtime": "import ctypes;ctypes.CDLL("
                + repr(str(root / "assets/rkllm_bridge.so"))
                + ")",
            }
            _command(
                [str(root / "bin/python"), "-I", "-c", probes[kind]],
                [environment_fd],
                kind,
                "runtime-import-probe",
            )
            versions = subprocess.run(
                [
                    str(root / "bin/python"),
                    "-I",
                    "-c",
                    'import importlib.metadata,json; print(json.dumps({d.metadata["Name"]:'
                    "d.version for d in importlib.metadata.distributions()}))",
                ],
                capture_output=True,
                timeout=30,
                check=True,
            )
            atomic_write(
                root / "ready.json",
                json.dumps(
                    {
                        "lock": identity,
                        "kind": kind,
                        "python": PYTHON_VERSION,
                        "build_environment": BUILD_ENVIRONMENTS.get(kind, {}),
                        "packages": json.loads(versions.stdout),
                        "assets": assets,
                        "preparation_probe": "import/load only; no model execution",
                        "bridge_sha256": hashlib.sha256(
                            read_file(root / "assets/rkllm_bridge.so")
                        ).hexdigest()
                        if kind == "rkllm-runtime"
                        else None,
                    }
                ).encode(),
            )
        except BaseException:
            # No ready marker means an interrupted environment cannot be used.
            raise
    return {
        "environment": kind,
        "lock": identity,
        "state": "ready",
        "license": "Rockchip SDK LICENSE applies" if assets else "See package licenses",
    }


def _command(command, retain_fds=(), kind=None, stage="managed-command"):
    environment = {
        **os.environ,
        "CMAKE_ARGS": "-DGGML_METAL=OFF -DGGML_CUDA=OFF -DGGML_VULKAN=OFF",
        **BUILD_ENVIRONMENTS.get(kind, {}),
    }
    process, connection, death_fd = launch_worker(
        sys.executable, retain_fds, command=command, environment=environment
    )
    try:
        handshake(process, connection, 30)
        if process.wait(timeout=1800) != 0:
            raise RkaiError(
                "environment_prepare_failed",
                "Managed environment preparation failed; check host/network/disk",
                3,
                details={
                    "program": Path(command[0]).name,
                    "stage": stage,
                    "exit_code": process.returncode,
                    "diagnostics": process.diagnostics.finish(),
                },
            )
    except subprocess.TimeoutExpired as e:
        raise RkaiError(
            "environment_prepare_timeout", "Environment preparation deadline exceeded"
        ) from e
    finally:
        connection.close()
        os.close(death_fd)
        stop(process)
