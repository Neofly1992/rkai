"""Static evidence only; never load native code or change system settings."""

import os
import platform
import shutil
import stat
import struct
import subprocess
import tempfile
from pathlib import Path

from .config import Config
from .errors import RkaiError
from .files import open_file
from .storage import Library


def doctor(
    config: Config,
    model: str | None = None,
    probe: bool = False,
    system_root: Path = Path("/"),
    load_runtime: bool = False,
) -> dict:
    checks = []
    active_probe = "not_run"

    def add(name, status, evidence, impact, remedy, required=True):
        checks.append(
            {
                "name": name,
                "status": status,
                "evidence": evidence,
                "impact": impact,
                "remedy": remedy,
                "required": required,
            }
        )

    add(
        "configuration",
        "pass",
        "Strict config validation succeeded",
        "Acquisition configuration is valid",
        "No action",
    )
    host = (platform.system().lower(), platform.machine().lower())
    supported_host = host == ("linux", "aarch64")
    add(
        "host",
        "pass" if supported_host else "fail",
        "/".join(host),
        "RK3588 NPU requires Linux aarch64; acquisition remains usable",
        "Run hardware checks on the target RK3588",
    )
    soc_path = system_root / "proc/device-tree/compatible"
    try:
        compatible = soc_path.read_bytes().replace(b"\x00", b", ").decode(errors="replace")
        soc = "rk3588" in compatible.lower()
        add(
            "soc",
            "pass" if soc else "fail",
            compatible[:512],
            "Target identity",
            "Use a board exposing RK3588 device-tree identity",
        )
    except OSError:
        add(
            "soc",
            "unknown",
            "Device-tree compatible is not readable",
            "Target identity unproven",
            "Check board OS/device tree",
        )
    nodes = list((system_root / "dev/dri").glob("renderD*"))
    npu_nodes = []
    for node in nodes:
        device = system_root / "sys/class/drm" / node.name / "device"
        try:
            if "rknpu" in (device / "driver").resolve().name.lower():
                npu_nodes.append(node)
        except OSError:
            pass
    legacy = system_root / "dev/rknpu"
    if legacy.exists():
        npu_nodes.append(legacy)
    try:
        accessible = bool(npu_nodes) and all(
            stat.S_ISCHR(n.stat().st_mode) and os.access(n, os.R_OK | os.W_OK) for n in npu_nodes
        )
    except OSError:
        accessible = False
    add(
        "npu_device",
        "pass" if accessible else "fail" if npu_nodes else "unknown",
        "Recognized NPU nodes: " + ", ".join(n.name for n in npu_nodes)
        if npu_nodes
        else "No positively identified rknpu device node",
        "Device permission and discovery",
        "Check BSP driver and user device permissions",
    )
    versions = [
        system_root / "sys/module/rknpu/version",
        system_root / "sys/kernel/debug/rknpu/version",
    ]
    driver = None
    for path in versions:
        try:
            driver = path.read_text().strip()[:512]
            if driver:
                break
        except OSError:
            pass
    add(
        "npu_driver",
        "pass" if driver else "unknown",
        driver or "No readable driver version",
        "Presence/version only; compatibility is separate",
        "Check BSP driver version interfaces",
    )
    root = Path(config.data_dir).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=root) as f:
            f.write(b"rkai doctor")
            f.flush()
            os.fsync(f.fileno())
        add(
            "data_directory",
            "pass",
            f"Writable; {shutil.disk_usage(root).free} bytes free",
            "Downloads require sufficient space",
            "No action",
        )
    except OSError:
        add(
            "data_directory",
            "fail",
            "Cannot create/write data directory",
            "Acquisition cannot persist",
            "Set an absolute writable --data-dir",
        )
    meminfo = system_root / "proc/meminfo"
    try:
        memory = next(
            line for line in meminfo.read_text().splitlines() if line.startswith("MemAvailable:")
        )
        add(
            "memory",
            "pass",
            memory,
            "Available memory measured; no model budget established",
            "Check model memory requirements before M2 execution",
            required=False,
        )
    except (OSError, StopIteration):
        add(
            "memory",
            "unknown",
            "MemAvailable unavailable",
            "Memory budget unknown",
            "Check target memory availability",
            required=False,
        )
    for backend, symbol in (("rknn", b"rknn_query"), ("rkllm", b"rkllm_init")):
        configured = config.runtime_paths.get(backend)
        if not configured:
            add(
                backend + "_runtime",
                "unknown",
                "No explicit runtime path configured",
                "Runtime file/ABI unverified",
                "Set runtime_paths to the official installed library",
            )
            continue
        path = Path(configured)
        try:
            with open_file(path.resolve()) as f:
                header = f.read(64)
            elf = header[:4] == b"\x7fELF" and header[4] == 2 and header[5] == 1
            machine = struct.unpack_from("<H", header, 18)[0] if elf else None
            valid = elf and machine == 183
            add(
                backend + "_elf",
                "pass" if valid else "fail",
                f"ELF64 little-endian AArch64: {valid}",
                "Runtime architecture",
                "Use the official Linux aarch64 runtime",
            )
            readelf = shutil.which("readelf")
            if readelf and valid:
                try:
                    result = subprocess.run(
                        [readelf, "--dyn-syms", "--wide", str(path)],
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                    found = result.returncode == 0 and any(
                        len(fields) >= 8
                        and fields[6] != b"UND"
                        and fields[7].split(b"@")[0] == symbol
                        for line in result.stdout.splitlines()
                        if (fields := line.split())
                    )
                    add(
                        backend + "_symbols",
                        "pass" if found else "unknown",
                        f"Expected API symbol present: {found}",
                        "API presence only",
                        "Verify the official runtime version and dynamic dependencies",
                    )
                except (OSError, subprocess.TimeoutExpired):
                    add(
                        backend + "_symbols",
                        "unknown",
                        "Static symbol inspection failed",
                        "API presence unproven",
                        "Inspect runtime with readelf",
                    )
            add(
                backend + "_loading",
                "unknown",
                "Static inspection does not execute native constructors",
                "Dependency loading and ABI compatibility unproven",
                "Validate explicit runtime in the M2 isolated worker",
            )
        except (OSError, RkaiError, IndexError, struct.error):
            add(
                backend + "_runtime",
                "fail",
                "Runtime file unreadable or invalid",
                "Runtime unavailable",
                "Configure a valid official runtime",
            )
    add(
        "compatibility",
        "unknown",
        "No hardware-validated compatibility profiles",
        "No NPU execution support is claimed",
        "Complete M2 model/runtime/driver validation",
    )
    if model:
        with Library(config) as library:
            inspection = library.inspect(model)
            integrity = inspection["file_state"] == "ready"
            add(
                "model_files",
                "pass" if integrity else "fail",
                "Selected plan size/SHA256 check",
                "File readiness is separate from hardware validation",
                "Reacquire damaged files",
            )
            add(
                "model_execution",
                "pass" if inspection["execution_validation"] == "passed" else "unknown",
                "Recorded NPU execution: " + inspection["execution_validation"],
                "Recorded execution only; semantic quality/compatibility is separate",
                "Run an installed probe on RK3588 and compare reference outputs",
            )
            contract = None
            try:
                from .execution import contract as execution_contract
                from .files import read_file
                from .manifest import MAX_MANIFEST_BYTES, parse

                with library.lease(model) as identity:
                    row = library.db.execute(
                        "SELECT plan_id FROM installations WHERE id=?", (identity,)
                    ).fetchone()
                    root = library.root / "installations" / identity
                    manifest = parse(read_file(root / "manifest.json", MAX_MANIFEST_BYTES))
                    plan = manifest.select(row["plan_id"], None)
                    contract, _, _ = execution_contract(plan, plan.default_capability)
                add(
                    "model_contract",
                    "pass",
                    "Registered adapter/backend configurations validated",
                    "Static contract only",
                    "Run --probe for actual execution",
                )
            except (RkaiError, OSError) as error:
                invalid = getattr(error, "code", None) in {
                    "invalid_adapter",
                    "invalid_backend",
                    "unsupported_capability",
                }
                add(
                    "model_contract",
                    "fail" if invalid else "unknown",
                    "Execution contract: " + getattr(error, "code", "unreadable"),
                    "No model execution evidence",
                    "Use a registered adapter/backend and fixed profile",
                )
            if probe and contract is not None and contract.probe is not None:
                try:
                    from .execution import Session

                    request = dict(contract.probe)
                    if isinstance(request.get("image"), str) and request["image"].startswith(
                        "file:"
                    ):
                        request["image"] = str(root / manifest.files[request["image"][5:]].path)
                    with Session(library, model) as session:
                        device = session.plan.target.accelerator
                        events = list(session.infer(request))
                        success = events[-1]["type"] == "completed"
                        add(
                            "dynamic_model_load",
                            "pass",
                            str(session.evidence),
                            "Native load succeeded for this context",
                            "No global ABI/quality guarantee",
                        )
                        add(
                            "probe",
                            "pass" if success else "fail",
                            device + ": installed adapter probe " + events[-1]["type"],
                            "Actual execution of this probe only",
                            "Compare with reference results",
                        )
                        active_probe = device + ("-passed" if success else "-failed")
                except (RkaiError, OSError, KeyError) as error:
                    add(
                        "probe",
                        "fail",
                        "Probe failed: " + getattr(error, "code", "input_or_storage_error"),
                        "Active execution failed",
                        "Inspect model/runtime/host and installed probe assets",
                    )
                    active_probe = "failed"
            elif probe:
                add(
                    "probe",
                    "skipped",
                    "Installed adapter has no declared probe",
                    "No active execution performed",
                    "Provide a valid probe in adapter config",
                    required=False,
                )
    if probe and not model:
        add(
            "probe",
            "skipped",
            "No installed model selected",
            "No active execution performed",
            "Use --model ALIAS with a probe-capable adapter",
            required=False,
        )
    if load_runtime:
        from .runtime_check import check_library

        for backend, configured in config.runtime_paths.items():
            result = check_library(Path(configured))
            add(
                backend + "_dynamic_load",
                result["status"],
                result["evidence"],
                "Dynamic loading only; no model execution",
                "Check ELF dependencies on target host",
            )
    required = [c["status"] for c in checks if c["required"]]
    status = (
        "unavailable" if "fail" in required else "degraded" if "unknown" in required else "ready"
    )
    return {"status": status, "checks": checks, "active_probe": active_probe}


def configuration_failure(error) -> dict:
    return {
        "status": "unavailable",
        "active_probe": "not_run",
        **error.result(),
        "checks": [
            {
                "name": "configuration",
                "status": "fail",
                "evidence": str(error),
                "impact": "Configured checks cannot proceed",
                "remedy": "Fix the specified TOML configuration",
                "required": True,
            }
        ],
    }
