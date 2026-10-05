"""Private worker entry; parent EOF forces exit even when native inference hangs."""

import os
import queue
import socket
import sys
import threading
from pathlib import Path

# -I excludes ambient PYTHONPATH/CWD. This path is the installed application's own code.
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rkai.adapters import ADAPTERS
from rkai.adapters import configuration as adapter_configuration
from rkai.backends import BACKENDS
from rkai.backends import configuration as backend_configuration
from rkai.diagnostics import exception_details
from rkai.errors import RkaiError
from rkai.files import read_file
from rkai.ipc import PROTOCOL, receive, send
from rkai.manifest import MAX_MANIFEST_BYTES, parse, relative_path
from rkai.rkllm import RkllmBackend
from rkai.rknn import RknnBackend


def execute(sock):
    commands = queue.Queue(maxsize=2)
    cancellations = {}
    closing = threading.Event()

    def read_commands():
        try:
            while not closing.is_set():
                message = receive(sock, timeout=86400)
                kind = message.get("type")
                request_id = message.get("request_id")
                if kind == "cancel":
                    event = cancellations.get(request_id)
                    if event:
                        event.set()
                else:
                    if kind == "infer":
                        cancellations[request_id] = threading.Event()
                    commands.put(message, timeout=10)
        except BaseException:
            if closing.is_set():
                return
            # EOF cannot depend on the blocked native thread acquiring the GIL or returning.
            os._exit(70)

    threading.Thread(target=read_commands, daemon=True).start()
    backends = {}
    adapter = None
    try:
        send(sock, {"type": "hello", "version": PROTOCOL, "worker_group": os.getpgrp()})
        while True:
            message = commands.get()
            kind = message["type"]
            if kind == "convert":
                from rkai.conversion import execute_conversion

                evidence = execute_conversion(
                    message["recipe"], Path(message["root"]), Path(message["output"])
                )
                send(sock, {"type": "converted", "evidence": evidence})
                break
            elif kind == "load":
                if adapter is not None:
                    raise RkaiError("protocol_error", "Worker already loaded")
                root = Path(message["root"])
                manifest = parse(read_file(root / "manifest.json", MAX_MANIFEST_BYTES))
                plan = manifest.select(message["plan"], None)
                cfg = adapter_configuration(plan.adapter)

                def asset(ref, manifest=manifest, root=root):
                    if not ref.startswith("file:") or ref[5:] not in manifest.files:
                        raise RkaiError("invalid_adapter", "Invalid adapter asset reference", 2)
                    return root / manifest.files[ref[5:]].path

                for component in plan.components:
                    backend_type = {**BACKENDS, "rknn": RknnBackend, "rkllm": RkllmBackend}[
                        component.backend.id
                    ]
                    backend_cfg = backend_configuration(component)
                    path = root / manifest.files[component.files["model"]].path
                    backends[component.id] = backend_type(
                        path,
                        backend_cfg,
                        Path(message["runtime"]) if message.get("runtime") else None,
                        cfg,
                    )
                adapter = ADAPTERS[plan.adapter.id](cfg, asset)
                send(
                    sock,
                    {"type": "loaded", "evidence": {k: v.evidence for k, v in backends.items()}},
                )
            elif kind == "infer":
                request_id = message["request_id"]
                cancel = cancellations[request_id]
                request = message["request"]
                if "image_ref" in request:
                    name = relative_path(request.pop("image_ref"))
                    request["image_path"] = str(Path(message["media_root"]) / name)
                send(sock, {"type": "started", "request_id": request_id})
                try:
                    for event, payload in adapter.infer(request, backends, cancel):
                        if cancel.is_set():
                            break
                        send(
                            sock,
                            {
                                "type": "output",
                                "request_id": request_id,
                                "kind": event,
                                "payload": payload,
                            },
                        )
                    send(
                        sock,
                        {
                            "type": "cancelled" if cancel.is_set() else "completed",
                            "request_id": request_id,
                        },
                    )
                except Exception as e:
                    send(
                        sock,
                        {
                            "type": "failed",
                            "request_id": request_id,
                            "error": e.result()["error"]
                            if isinstance(e, RkaiError)
                            else {
                                "code": "backend_error",
                                "message": "Native/adapter execution failed",
                                "exit_code": 1,
                                "details": exception_details(e),
                            },
                        },
                    )
                finally:
                    cancellations.pop(request_id, None)
            elif kind == "shutdown":
                break
            else:
                raise RkaiError("protocol_error", "Unsupported worker command")
    except Exception as e:
        send(
            sock,
            {
                "type": "failed",
                "error": e.result()["error"]
                if isinstance(e, RkaiError)
                else {
                    "code": "worker_load_failed",
                    "message": "Worker could not load model/runtime",
                    "exit_code": 3,
                    "details": exception_details(e),
                },
            },
        )
    finally:
        for backend in reversed(list(backends.values())):
            backend.close()
        closing.set()
        sock.close()


if __name__ == "__main__":
    execute(socket.socket(fileno=int(sys.argv[1])))
