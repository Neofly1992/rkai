"""Owned process groups: prove native descendants stopped before releasing resources."""

import os
import signal
import subprocess
import time

from .errors import RkaiError
from .ipc import CANCEL_GRACE_SECONDS, PROTOCOL, receive


def group_alive(group: int) -> bool:
    # Inspect only numeric group and state, never command lines or user input.
    result = subprocess.run(
        ["ps", "-axo", "pgid=,stat="], capture_output=True, timeout=5, check=False
    )
    if result.returncode:
        raise RkaiError("cleanup_unverified", "Cannot establish worker group termination")
    return any(
        int(parts[0]) == group and not parts[1].startswith(b"Z")
        for line in result.stdout.splitlines()
        if len(parts := line.split()) == 2
    )


def stop_group(group: int, grace: float = CANCEL_GRACE_SECONDS):
    if not group_alive(group):
        return
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, signum)
        except ProcessLookupError:
            return
        except OSError as e:
            if not group_alive(group):
                return
            raise RkaiError("cleanup_unverified", "Cannot stop native group; lease retained") from e
        deadline = time.monotonic() + grace
        while group_alive(group):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        else:
            return
    raise RkaiError("cleanup_unverified", "Native group did not stop; lease retained")


def stop(process: subprocess.Popen, grace: float = CANCEL_GRACE_SECONDS):
    native = getattr(process, "native_group", None)
    if native is not None:
        # Guardian retains lock descriptors until this independent native group stops.
        stop_group(native, grace)
    if group_alive(process.pid):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired as e:
        if hasattr(process, "native_group") and native is None:
            # Before handshake only guardian knows its child; never kill the lock keeper
            # while termination of that child remains unproved.
            raise RkaiError(
                "cleanup_unverified", "Guardian cleanup incomplete; lease retained"
            ) from e
        stop_group(process.pid, grace)
        process.wait(timeout=grace)
    if group_alive(process.pid):
        stop_group(process.pid, grace)
    if hasattr(process, "diagnostics"):
        process.diagnostics.finish()


def handshake(process, sock, timeout, cancel=None):
    hello = receive(sock, timeout, cancel=cancel)
    group = hello.get("worker_group")
    if (hello.get("type"), hello.get("version")) != ("hello", PROTOCOL) or (
        type(group) is not int or group <= 1 or group == process.pid
    ):
        raise RkaiError("protocol_error", "Worker handshake/group identity mismatch")
    process.native_group = group


def launch_worker(python, retain_fds=(), command=None, environment=None):
    """Return owned guardian, socket and parent-death writer; all descriptors explicit."""
    import socket
    from pathlib import Path

    if not group_alive(os.getpgrp()):
        raise RkaiError(
            "process_inspection_unavailable", "Install a ps with PGID/state inspection support", 3
        )
    parent, child = socket.socketpair()
    death_read, death_write = os.pipe()
    try:
        process = subprocess.Popen(
            [
                str(python),
                "-I",
                str(Path(__file__).with_name("supervisor.py")),
                str(child.fileno()),
                str(death_read),
                *[str(fd) for fd in retain_fds],
                *(["--", *command] if command is not None else []),
            ],
            pass_fds=(child.fileno(), death_read, *retain_fds),
            env={**(environment or os.environ), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        process.native_group = None
        from .diagnostics import NativeDiagnostics

        process.diagnostics = NativeDiagnostics((process.stdout, process.stderr))
        return process, parent, death_write
    except BaseException:
        parent.close()
        os.close(death_write)
        raise
    finally:
        child.close()
        os.close(death_read)
