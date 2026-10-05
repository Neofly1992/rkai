"""One private socketpair protocol; bounded frames, no transport abstraction."""

import json
import select
import socket
import struct
import time

from .errors import RkaiError
from .manifest import no_duplicates, reject_constant

PROTOCOL = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024
IO_TIMEOUT_SECONDS = 10.0
CANCEL_GRACE_SECONDS = 5.0
LOAD_TIMEOUT_SECONDS = 120.0
REQUEST_TIMEOUT_SECONDS = 300.0
QUEUE_EVENTS = 64
QUEUE_BYTES = MAX_FRAME_BYTES


def encode(message: dict) -> bytes:
    try:
        raw = json.dumps(message, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError) as e:
        raise RkaiError("invalid_frame", "IPC message is not finite JSON", 2) from e
    if len(raw) > MAX_FRAME_BYTES:
        raise RkaiError("frame_too_large", "IPC frame exceeds 8 MiB", 2)
    return struct.pack("!I", len(raw)) + raw


def send(sock: socket.socket, message: dict):
    # Nonblocking descriptor is essential: writability does not promise a whole frame fits.
    sock.setblocking(False)
    raw = memoryview(encode(message))
    deadline = time.monotonic() + IO_TIMEOUT_SECONDS
    while raw:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([], [sock], [], max(0, left))[1]:
            raise RkaiError("ipc_timeout", "Worker write deadline exceeded")
        try:
            count = sock.send(raw)
        except BlockingIOError:
            continue
        if count == 0:
            raise EOFError
        raw = raw[count:]


def receive(sock: socket.socket, timeout: float = IO_TIMEOUT_SECONDS) -> dict:
    sock.setblocking(False)
    deadline = time.monotonic() + timeout

    def exact(size):
        raw = bytearray()
        while len(raw) < size:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([sock], [], [], max(0, left))[0]:
                raise RkaiError("ipc_timeout", "Worker read deadline exceeded")
            try:
                chunk = sock.recv(size - len(raw))
            except BlockingIOError:
                continue
            if not chunk:
                raise EOFError
            raw.extend(chunk)
        return raw

    size = struct.unpack("!I", exact(4))[0]
    if not 0 < size <= MAX_FRAME_BYTES:
        raise RkaiError("invalid_frame", "Invalid IPC length")
    try:
        result = json.loads(
            exact(size), object_pairs_hook=no_duplicates, parse_constant=reject_constant
        )
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError) as e:
        raise RkaiError("invalid_frame", "Invalid IPC JSON") from e
