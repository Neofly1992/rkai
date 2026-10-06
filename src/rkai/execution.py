"""Leases, host/resource admission and bounded session/event lifecycle."""

import logging
import os
import subprocess
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from .adapters import ADAPTER_BACKENDS, ADAPTER_CAPABILITIES, ImageConfig, image_workspace_bytes
from .adapters import configuration as adapter_configuration
from .backends import ACCELERATORS, ENVIRONMENTS
from .backends import configuration as backend_configuration
from .capabilities import FEATURES, MAX_IMAGE_BYTES, validate_request, validate_result
from .environments import host, resolve
from .errors import RkaiError
from .files import ensure_directory, open_file, read_file, remove_tree
from .ipc import (
    CANCEL_GRACE_SECONDS,
    IO_TIMEOUT_SECONDS,
    LOAD_TIMEOUT_SECONDS,
    QUEUE_BYTES,
    QUEUE_EVENTS,
    REQUEST_TIMEOUT_SECONDS,
    encode,
    receive,
    send,
)
from .manifest import MAX_MANIFEST_BYTES, parse
from .processes import handshake, launch_worker, stop
from .storage import lock

MIB = 1024 * 1024


def available_memory():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    # macOS exposes total memory; use an explicitly configured budget there, not free-memory claims.
    return None


def contract(plan, capability):
    cfg = adapter_configuration(plan.adapter)
    if capability not in ADAPTER_CAPABILITIES[plan.adapter.id]:
        raise RkaiError(
            "unsupported_capability", "Adapter cannot provide the requested capability", 3
        )
    declarations = {c.id: c for c in plan.capabilities}
    if capability not in declarations or any(
        c.id not in ADAPTER_CAPABILITIES[plan.adapter.id] or set(c.features) - FEATURES[c.id]
        for c in plan.capabilities
    ):
        raise RkaiError(
            "unsupported_capability", "Unsupported capability/features in installed plan", 3
        )
    if cfg.component not in {c.id for c in plan.components}:
        raise RkaiError("invalid_adapter", "Adapter component is missing", 2)
    if len(plan.components) != 1:
        raise RkaiError(
            "unsupported_composition", "Current adapters require one model component", 3
        )
    if any(c.backend.id not in ADAPTER_BACKENDS[plan.adapter.id] for c in plan.components):
        raise RkaiError("invalid_adapter", "Adapter/backend execution semantics disagree", 2)
    configs = [backend_configuration(c) for c in plan.components]
    backend_ids = {c.backend.id for c in plan.components}
    if any(ACCELERATORS[b] != plan.target.accelerator for b in backend_ids):
        raise RkaiError("invalid_backend", "Backend and accelerator target disagree", 2)
    environments = {ENVIRONMENTS[b] for b in backend_ids}
    return cfg, configs, environments.pop()


def admit(library, plan, capability, allow_cpu):
    cfg, configs, environment_kind = contract(plan, capability)
    target = plan.target
    if (target.os, target.arch) != host():
        raise RkaiError("unsupported_host", "Installed plan does not match this host", 3)
    if target.accelerator == "cpu" and not allow_cpu:
        raise RkaiError("cpu_fallback_disabled", "CPU execution requires --allow-cpu-fallback", 3)
    if target.accelerator == "rockchip-npu":
        try:
            if b"rk3588" not in Path("/proc/device-tree/compatible").read_bytes():
                raise RkaiError("unsupported_soc", "Runtime target is not RK3588", 3)
        except OSError as e:
            raise RkaiError("unknown_soc", "RK3588 device-tree identity is required", 3) from e
        # Reuse static device detection; do not turn permission failure into CPU fallback.
        from .doctor import doctor

        result = doctor(library.config)
        node = next(c for c in result["checks"] if c["name"] == "npu_device")
        if node["status"] != "pass":
            raise RkaiError(
                "npu_permission" if node["status"] == "fail" else "npu_absent",
                "NPU device/permissions must pass doctor",
                3,
            )
    memory = sum(c.memory_bytes for c in configs) + getattr(library, "execution_overhead_bytes", 0)
    if isinstance(cfg, ImageConfig):
        memory += image_workspace_bytes(cfg)
    budget = library.config.memory_budget_mb * MIB
    free = available_memory()
    if memory > budget or (
        free is not None and memory + library.config.memory_reserve_mb * MIB > free
    ):
        raise RkaiError(
            "resource_admission", "Declared load/request peak exceeds memory allowance", 4
        )
    environment = resolve(library, environment_kind)
    return cfg, environment, memory


class EventBuffer:
    """Both count and bytes are bounded; receiver cannot grow memory without a consumer."""

    def __init__(self):
        self.condition = threading.Condition()
        self.events = []
        self.bytes = 0
        self.failure = None

    def put(self, event):
        size = len(encode(event))
        deadline = time.monotonic() + IO_TIMEOUT_SECONDS
        with self.condition:
            while len(self.events) >= QUEUE_EVENTS or self.bytes + size > QUEUE_BYTES:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RkaiError("backpressure", "Consumer did not drain bounded output")
                self.condition.wait(left)
            self.events.append((event, size))
            self.bytes += size
            self.condition.notify_all()

    def get(self):
        with self.condition:
            while not self.events and self.failure is None:
                self.condition.wait()
            if self.events:
                event, size = self.events.pop(0)
                self.bytes -= size
                self.condition.notify_all()
                return event
            raise self.failure

    def fail(self, error):
        with self.condition:
            self.events.clear()
            self.bytes = 0
            self.failure = error
            self.condition.notify_all()


class Session:
    def __init__(
        self,
        library,
        alias,
        capability=None,
        allow_cpu=None,
        *,
        load_cancel=None,
        load_timeout=LOAD_TIMEOUT_SECONDS,
    ):
        self.library = library
        load_deadline = time.monotonic() + min(LOAD_TIMEOUT_SECONDS, load_timeout)

        def load_remaining():
            if load_cancel is not None and load_cancel.is_set():
                raise RkaiError("cancelled", "Worker load cancelled", 130)
            left = load_deadline - time.monotonic()
            if left <= 0:
                raise RkaiError("ipc_timeout", "Worker load deadline exceeded")
            return left

        self.id = uuid.uuid4().hex
        self.stack = ExitStack()
        self.retain_fds = []
        self.process = None
        self.sock = None
        self.death_fd = None
        self.closed = False
        self.loaded = False
        self.active = None
        self.worker_terminal = None
        self.cancel_requested = None
        self.history = []
        self.guard = threading.RLock()
        self.send_guard = threading.Lock()
        try:
            self.identity = self.stack.enter_context(library.lease(alias, self.retain_fds))
            row = library.db.execute(
                "SELECT * FROM installations WHERE id=?", (self.identity,)
            ).fetchone()
            self.root = library.root / "installations" / self.identity
            manifest = parse(read_file(self.root / "manifest.json", MAX_MANIFEST_BYTES))
            self.plan = manifest.select(row["plan_id"], None)
            library._verify_directory(
                self.root,
                manifest,
                {
                    "snapshot": {"manifest_hash": row["manifest_hash"]},
                    "files": manifest.closure(self.plan),
                },
            )
            self.capability = capability or self.plan.default_capability
            allowed = library.config.allow_cpu_fallback if allow_cpu is None else allow_cpu
            self.selection_reason = "installed NPU snapshot"
            try:
                self.adapter_config, environment, self.memory_bytes = admit(
                    library, self.plan, self.capability, allowed
                )
            except RkaiError as error:
                if (
                    not allowed
                    or self.plan.target.accelerator != "rockchip-npu"
                    or error.code
                    not in {"unsupported_host", "unsupported_soc", "unknown_soc", "npu_absent"}
                ):
                    raise
                original = error.code
                candidates = []
                for row2 in library.db.execute(
                    "SELECT i.*,a.name AS alias FROM installations i JOIN aliases a "
                    "ON i.id=a.installation WHERE i.manifest_hash=? AND i.id!=?",
                    (row["manifest_hash"], self.identity),
                ).fetchall():
                    candidate = manifest.select(row2["plan_id"], None)
                    if (
                        candidate.target.accelerator == "cpu"
                        and (candidate.target.os, candidate.target.arch) == host()
                        and self.capability in {c.id for c in candidate.capabilities}
                        and (capability or candidate.default_capability) == self.capability
                    ):
                        if row2["id"] not in {r["id"] for r in candidates}:
                            candidates.append(row2)
                if not candidates:
                    raise RkaiError(
                        "cpu_artifact_missing",
                        "No installed CPU plan for this snapshot; explicitly pull its CPU plan",
                        3,
                    ) from error
                if len(candidates) != 1:
                    raise RkaiError(
                        "ambiguous_plan",
                        "Multiple installed CPU plans; run their explicit alias",
                        2,
                    ) from error
                selected = candidates[0]
                self.identity = self.stack.enter_context(
                    library.lease(selected["alias"], self.retain_fds)
                )
                self.root = library.root / "installations" / self.identity
                self.plan = manifest.select(selected["plan_id"], None)
                library._verify_directory(
                    self.root,
                    manifest,
                    {
                        "snapshot": {"manifest_hash": selected["manifest_hash"]},
                        "files": manifest.closure(self.plan),
                    },
                )
                self.adapter_config, environment, self.memory_bytes = admit(
                    library, self.plan, self.capability, True
                )
                self.selection_reason = "NPU unavailable before execution: " + original
            if (
                self.plan.target.accelerator == "cpu"
                and self.selection_reason == "installed NPU snapshot"
            ):
                self.selection_reason = "explicit CPU permission"

            # One active plan in this UID's host admission domain, across data directories.
            device_root = Path("/tmp").resolve() / f"rkai-{os.getuid()}"
            ensure_directory(device_root)
            if device_root.stat().st_uid != os.getuid():
                raise RkaiError(
                    "unsafe_device_lock", "Device lock directory has a different owner", 3
                )
            device_root.chmod(0o700)
            self.retain_fds.append(self.stack.enter_context(lock(device_root / "host-execution")))
            if self.plan.target.accelerator == "rockchip-npu":
                self.retain_fds.append(self.stack.enter_context(lock(device_root / "rk3588-npu")))
            self.media_root = library.root / "sessions" / self.id
            ensure_directory(self.media_root)
            self.stack.callback(remove_tree, self.media_root)
            load_remaining()
            self.process, parent, self.death_fd = launch_worker(
                environment / "bin/python", self.retain_fds
            )
            self.sock = parent
            handshake(self.process, parent, load_remaining(), cancel=load_cancel)
            backend_ids = {c.backend.id for c in self.plan.components}
            runtime = (
                environment / "assets/librknnrt.so"
                if "rknn" in backend_ids
                else (environment / "assets/librkllmrt.so" if "rkllm" in backend_ids else None)
            )
            send(
                parent,
                {
                    "type": "load",
                    "root": str(self.root),
                    "plan": self.plan.id,
                    "runtime": str(runtime) if runtime else None,
                },
                timeout=load_remaining(),
                cancel=load_cancel,
            )
            loaded = receive(parent, load_remaining(), cancel=load_cancel)
            if loaded.get("type") != "loaded":
                raise RkaiError(
                    "worker_load_failed",
                    "Runtime/model load failed; no fallback replay",
                    3,
                    details={"worker_code": loaded.get("error", {}).get("code", "unknown")},
                )
            self.evidence = loaded["evidence"]
            self.loaded = True

            def guardian_exit():
                self.process.wait()
                # A dead guardian may leave a native child holding the socket open.
                # Stop the whole group promptly, rather than waiting for request timeout.
                try:
                    self.close()
                except RkaiError as error:
                    logging.getLogger("rkai.execution").error(
                        "session_id=%s cleanup=%s lease=retained", self.id, error.code
                    )

            threading.Thread(target=guardian_exit, daemon=True).start()
        except BaseException:
            try:
                self.close()
            except RkaiError as cleanup:
                # An application owner must retain this context when stop cannot be proved.
                cleanup.session = self
                raise
            raise

    def _send(self, message):
        with self.send_guard:
            send(self.sock, message)

    def infer(self, data):
        request = validate_request(self.capability, data)
        with self.guard:
            if self.closed or self.active:
                raise RkaiError("busy", "Session closed or already executing", 4)
            request_id = uuid.uuid4().hex
            self.active = request_id
            self.worker_terminal = None
            self.cancel_requested = None
        wire = request.model_dump()
        try:
            if self.capability == "classification":
                source = Path(wire.pop("image")).expanduser()
                source = source.parent.resolve() / source.name
                # Secure bounded copy. No user path is sent to worker.
                name = request_id + ".image"
                with open_file(source) as src, open_file(self.media_root / name, "wb") as dest:
                    if os.fstat(src.fileno()).st_size > MAX_IMAGE_BYTES:
                        raise RkaiError("input_too_large", "Image exceeds 16 MiB", 2)
                    remaining = MAX_IMAGE_BYTES
                    while chunk := src.read(min(65536, remaining + 1)):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise RkaiError("input_too_large", "Image grew beyond 16 MiB", 2)
                        dest.write(chunk)
                wire["image_ref"] = name
            # Verify frame budget before accepting a request.
            command = {
                "type": "infer",
                "request_id": request_id,
                "request": wire,
                "media_root": str(self.media_root),
            }
            encode(command)
            if self.process.poll() is not None:
                raise RkaiError("worker_crashed", "Worker guardian exited")
            self._send(command)
        except BaseException:
            self.active = None
            self.close()
            raise
        buffer = EventBuffer()

        def pump():
            try:
                deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
                while True:
                    if self.process.poll() is not None:
                        raise RkaiError("worker_crashed", "Worker guardian exited")
                    event = receive(self.sock, max(0.001, deadline - time.monotonic()))
                    if event.get("request_id") != request_id or event.get("type") not in {
                        "started",
                        "output",
                        "completed",
                        "cancelled",
                        "failed",
                    }:
                        raise RkaiError("protocol_error", "Unexpected worker request event")
                    if event["type"] in {"completed", "cancelled", "failed"}:
                        if event["type"] == "failed" and (
                            type(event.get("error", {}).get("exit_code")) is not int
                            or event["error"]["exit_code"] not in {1, 2, 3, 4, 130}
                        ):
                            raise RkaiError("protocol_error", "Invalid worker error exit status")
                        self.worker_terminal = request_id
                    buffer.put(event)
                    if event["type"] in {"completed", "cancelled", "failed"}:
                        return
            except BaseException as e:
                try:
                    self.close()
                finally:
                    buffer.fail(
                        e
                        if isinstance(e, RkaiError)
                        else RkaiError("worker_crashed", "Worker disconnected or crashed")
                    )

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        sequence = 0
        final_result = None
        started = False
        terminal = False
        try:
            yield {
                "session_id": self.id,
                "request_id": request_id,
                "sequence": sequence,
                "type": "accepted",
                "plan": self.plan.id,
                "device": self.plan.target.accelerator,
                "runtime": self.evidence,
                "selection_reason": self.selection_reason,
            }
            while True:
                event = buffer.get()
                kind = event["type"]
                if kind == "started":
                    if started:
                        raise RkaiError("protocol_error", "Duplicate started event")
                    started = True
                elif not started:
                    raise RkaiError("protocol_error", "Worker event before started")
                if kind == "output":
                    if event.get("kind") == "result":
                        if final_result is not None:
                            raise RkaiError("protocol_error", "Duplicate capability result")
                        final_result = validate_result(self.capability, event["payload"], request)
                        event["payload"] = final_result
                    elif event.get("kind") == "delta" and self.capability == "chat":
                        from .capabilities import ChatDelta

                        event["payload"] = ChatDelta.model_validate(event["payload"]).model_dump()
                    else:
                        raise RkaiError("protocol_error", "Unknown output kind")
                if kind in {"failed", "cancelled"}:
                    # Native errors retire the context. Reap before reporting terminal state.
                    self.close()
                    if kind == "failed":
                        details = event["error"].setdefault("details", {})
                        tags = set(details.get("diagnostics", []))
                        tags.update(self.process.diagnostics.snapshot())
                        details["diagnostics"] = sorted(tags)
                if kind == "completed" and final_result is None:
                    raise RkaiError("protocol_error", "Completion without capability result")
                sequence += 1
                terminal = kind in {"completed", "cancelled", "failed"}
                if terminal and kind == "completed" and self.capability == "chat":
                    self.history = wire["messages"] + [
                        {"role": "assistant", "content": final_result["text"]}
                    ]
                if terminal:
                    self.active = None
                    if kind == "completed":
                        self.library.record_execution(
                            self.identity,
                            self.id,
                            {
                                "device": self.plan.target.accelerator,
                                "plan": self.plan.id,
                                "runtime": self.evidence,
                                "capability": self.capability,
                                "execution": "passed",
                                "semantic_reference": "not_run",
                            },
                        )
                yield {"session_id": self.id, "sequence": sequence, **event}
                if terminal:
                    return
        except RkaiError as e:
            self.close()
            sequence += 1
            yield {
                "session_id": self.id,
                "request_id": request_id,
                "sequence": sequence,
                "type": "cancelled" if self.cancel_requested == request_id else "failed",
                "error": e.result()["error"] if self.cancel_requested != request_id else None,
            }
        finally:
            if not terminal:
                self.close()
            reader.join(timeout=CANCEL_GRACE_SECONDS + 1)
            self.active = None
            if self.media_root.exists():
                from .files import unlink_file

                unlink_file(self.media_root / (request_id + ".image"), missing_ok=True)

    def cancel(self, request_id=None):
        with self.guard:
            if request_id is not None and self.active != request_id:
                return
            request_id = self.active
            if not request_id or self.closed or self.worker_terminal == request_id:
                return
            self.cancel_requested = request_id
            try:
                self._send({"type": "cancel", "request_id": request_id})
            except (OSError, RkaiError):
                self.close()
                return

        def deadline():
            with self.guard:
                if self.active == request_id and self.worker_terminal != request_id:
                    self.close()

        timer = threading.Timer(CANCEL_GRACE_SECONDS, deadline)
        timer.daemon = True
        timer.start()

    def reset(self):
        if self.active:
            raise RkaiError("busy", "Cannot reset an active request", 4)
        self.history.clear()

    def close(self):
        with self.guard:
            if self.closed:
                return
            if self.process is not None:
                # Before loaded, the worker may be stuck in startup/constructor:
                # a graceful command cannot prove progress, stop the owned group.
                if self.loaded and self.sock is not None and not self.active:
                    try:
                        self._send({"type": "shutdown"})
                        self.process.wait(timeout=CANCEL_GRACE_SECONDS)
                    except (OSError, RkaiError, subprocess.TimeoutExpired):
                        pass
                stop(self.process)
            if self.death_fd is not None:
                os.close(self.death_fd)
                self.death_fd = None
            if self.sock is not None:
                self.sock.close()
            self.stack.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
