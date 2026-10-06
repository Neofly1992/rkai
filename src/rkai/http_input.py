"""Count actual bytes; multipart media never becomes an IPC control payload."""

import json
import re
import shutil
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header

from .errors import RkaiError
from .files import ensure_directory, open_file, remove_tree, unlink_file
from .ipc import IO_TIMEOUT_SECONDS
from .manifest import no_duplicates, reject_constant
from .service import INPUT_MEMORY_FACTOR, POLL_SECONDS
from .storage import lock

READ_CHUNK_BYTES = 64 * 1024
HEADER_BYTES = 16 * 1024


@contextmanager
def upload_registry(root):
    """Serialize owner creation/removal so unlink cannot split a live flock inode.

    One stable registry lock remains; per-upload UUID locks are temporary. File
    operations run off ASGI and wait only for this short registry transaction.
    """
    deadline = time.monotonic() + IO_TIMEOUT_SECONDS
    with ExitStack() as owned:
        while True:
            try:
                owned.enter_context(lock(root / "locks/http-uploads"))
                break
            except RkaiError as error:
                if error.code != "busy" or time.monotonic() >= deadline:
                    raise
                time.sleep(POLL_SECONDS)
        yield


def json_object(raw):
    try:
        data = json.loads(raw, object_pairs_hook=no_duplicates, parse_constant=reject_constant)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, UnicodeError, RecursionError) as error:
        raise RkaiError("invalid_input", "Invalid JSON object", 2) from error


class Multipart:
    """Incremental parser with one JSON metadata field and one image file.

    Unknown parts are rejected before staging. Filename never determines a path.
    Parser/file access is sequential; its owner awaits each chunk operation.
    """

    def __init__(self, content_type, limits, reservation, root):
        self.limits, self.reservation = limits, reservation
        _, options = parse_options_header(content_type)
        boundary = options.get(b"boundary", b"")
        if not boundary or len(boundary) > 200:
            raise RkaiError("invalid_input", "Invalid multipart boundary", 2)
        self.owned = ExitStack()
        identity = uuid.uuid4().hex
        self.root = Path(root).expanduser().resolve()
        self.owner_path = self.root / "locks" / ("http-upload-" + identity)
        staging = self.root / "http-staging"
        self.directory = staging / identity
        try:
            with upload_registry(self.root):
                ensure_directory(staging)
                staging.chmod(0o700)
                self.owned.enter_context(lock(self.owner_path))
                try:
                    ensure_directory(self.directory)
                    self.directory.chmod(0o700)
                except BaseException:
                    self.owned.close()
                    unlink_file(self.owner_path, missing_ok=True)
                    raise
        except BaseException:
            self.owned.close()
            raise
        self.media = self.directory / "image"
        self.file = None
        self.metadata = bytearray()
        self.seen = set()
        self.count = 0
        self.ended = False
        self.parser = MultipartParser(
            boundary,
            {
                "on_part_begin": self.begin,
                "on_header_field": self.header_field,
                "on_header_value": self.header_value,
                "on_header_end": self.header_end,
                "on_headers_finished": self.headers_finished,
                "on_part_data": self.part_data,
                "on_part_end": self.part_end,
                "on_end": self.end,
            },
        )

    def begin(self):
        self.headers = {}
        self.field = bytearray()
        self.value = bytearray()
        self.header_size = self.size = 0

    def header_field(self, data, start, end):
        self.header_size += end - start
        self.check_headers()
        self.field.extend(data[start:end])

    def header_value(self, data, start, end):
        self.header_size += end - start
        self.check_headers()
        self.value.extend(data[start:end])

    def check_headers(self):
        if self.header_size > HEADER_BYTES:
            raise RkaiError("input_too_large", "Multipart headers exceed budget", 2)

    def header_end(self):
        key = bytes(self.field).lower()
        if key in self.headers:
            raise RkaiError("invalid_input", "Duplicate multipart header", 2)
        self.headers[key] = bytes(self.value)
        self.field.clear()
        self.value.clear()

    def headers_finished(self):
        kind, options = parse_options_header(self.headers.get(b"content-disposition", b""))
        self.name = options.get(b"name")
        if (
            kind != b"form-data"
            or self.name not in {b"metadata", b"image"}
            or self.name in self.seen
        ):
            raise RkaiError("invalid_input", "Expected unique metadata and image parts", 2)
        self.seen.add(self.name)
        if self.name == b"image":
            self.count += 1
            if self.count > self.limits.media_count:
                raise RkaiError("input_too_large", "Too many media assets", 2)
            self.file = self.owned.enter_context(open_file(self.media, "wb"))

    def part_data(self, data, start, end):
        length = end - start
        self.size += length
        limit = (
            self.limits.metadata_bytes if self.name == b"metadata" else self.limits.media_file_bytes
        )
        if self.size > limit:
            raise RkaiError("input_too_large", "Multipart part exceeds budget", 2)
        if self.name == b"metadata":
            self.reservation.add(memory=length * INPUT_MEMORY_FACTOR)
            self.metadata.extend(data[start:end])
        else:
            # Reserve both upload and Session's bounded private copy, including queued media.
            if shutil.disk_usage(self.directory).free < length * 2:
                raise RkaiError("resource_admission", "Insufficient upload disk space", 4)
            self.reservation.add(disk=length * 2)
            self.file.write(data[start:end])

    def part_end(self):
        if self.file is not None:
            self.file.close()
            self.file = None

    def end(self):
        self.ended = True

    def finish(self):
        self.parser.finalize()
        if not self.ended or self.seen != {b"metadata", b"image"}:
            raise RkaiError("invalid_input", "Incomplete multipart request", 2)
        return json_object(self.metadata), self.media

    def close(self):
        with upload_registry(self.root):
            if self.file:
                self.file.close()
            if self.directory.exists():
                remove_tree(self.directory)
            self.owned.close()
            unlink_file(self.owner_path, missing_ok=True)


def recover_uploads(root):
    """Recover private UUID uploads/orphan locks, preserving active flock owners.

    This also removes ephemeral orphan locks from the earlier M3 implementation;
    installations, model leases and unrelated files are never swept.
    """
    with upload_registry(root):
        staging = root / "http-staging"
        ensure_directory(staging)
        staging.chmod(0o700)
        identities = {d.name for d in staging.iterdir() if re.fullmatch(r"[0-9a-f]{32}", d.name)}
        identities.update(
            p.name.removeprefix("http-upload-")
            for p in (root / "locks").iterdir()
            if re.fullmatch(r"http-upload-[0-9a-f]{32}", p.name)
        )
        for identity in identities:
            owner_path = root / "locks" / ("http-upload-" + identity)
            try:
                with lock(owner_path):
                    directory = staging / identity
                    if directory.exists():
                        remove_tree(directory)
                    unlink_file(owner_path)
            except RkaiError as error:
                if error.code != "busy":
                    raise
