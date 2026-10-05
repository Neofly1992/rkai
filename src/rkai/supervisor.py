"""GIL-independent parent-death guardian and retained resource-lock owner."""

import os
import select
import signal
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rkai.ipc import PROTOCOL, send
from rkai.processes import group_alive, stop_group


def main():
    ipc_fd, death_fd = int(sys.argv[1]), int(sys.argv[2])
    arguments = sys.argv[3:]
    separator = arguments.index("--") if "--" in arguments else len(arguments)
    retained = tuple(int(fd) for fd in arguments[:separator])
    command = arguments[separator + 1 :] if separator < len(arguments) else None
    stopping = False

    def terminate(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, terminate)
    worker = subprocess.Popen(
        command or [sys.executable, "-I", str(Path(__file__).with_name("worker.py")), str(ipc_fd)],
        pass_fds=(ipc_fd, *retained),
        start_new_session=True,
    )
    if command is not None:
        import socket

        with socket.socket(fileno=ipc_fd) as connection:
            send(connection, {"type": "hello", "version": PROTOCOL, "worker_group": worker.pid})
    else:
        os.close(ipc_fd)
    try:
        while worker.poll() is None:
            if stopping or (select.select([death_fd], [], [], 0.1)[0] and not os.read(death_fd, 1)):
                stop_group(worker.pid)
                return worker.wait()
        # SDK descendants may outlive the main native process.
        stop_group(worker.pid)
        return worker.wait()
    finally:
        os.close(death_fd)
        # Retained shared/exclusive flock descriptions are not unlocked here.
        # Process exit closes them only after native termination is established.
        if group_alive(worker.pid):
            # A native process stuck in kernel teardown must keep the locks held.
            while group_alive(worker.pid):
                select.select([], [], [], 0.1)


if __name__ == "__main__":
    sys.exit(main())
