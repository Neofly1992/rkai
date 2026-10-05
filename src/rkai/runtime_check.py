"""Opt-in native loading in an owned subprocess; never equate dlopen with inference."""

import os
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rkai.errors import RkaiError
from rkai.files import open_file
from rkai.processes import handshake, launch_worker, stop

LOAD_TIMEOUT_SECONDS = 10


def check_library(path):
    try:
        with open_file(path) as source:
            source.read(64)
    except (OSError, RkaiError):
        return {"status": "fail", "evidence": "Runtime path is unreadable or unsafe"}
    process, connection, death_fd = launch_worker(
        sys.executable,
        command=[sys.executable, "-I", str(Path(__file__).resolve()), str(path)],
    )
    try:
        handshake(process, connection, LOAD_TIMEOUT_SECONDS)
        if process.wait(timeout=LOAD_TIMEOUT_SECONDS) == 0:
            return {
                "status": "pass",
                "evidence": "Native constructors/dependencies loaded in isolation",
            }
        return {"status": "fail", "evidence": "Isolated native loading failed"}
    except subprocess.TimeoutExpired:
        return {"status": "fail", "evidence": "Isolated native loading timed out"}
    finally:
        connection.close()
        os.close(death_fd)
        stop(process)


if __name__ == "__main__":
    import ctypes

    try:
        ctypes.CDLL(sys.argv[1])
    except OSError:
        sys.exit(3)
