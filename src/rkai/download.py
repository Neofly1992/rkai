"""Bounded, resumable HTTP transfers with strong validators and final SHA256."""

import hashlib
import json
import logging
import os
from pathlib import Path

import httpx

from .errors import RkaiError
from .files import (
    directory_fd,
    ensure_directory,
    file_size,
    open_file,
    read_file,
    replace_file,
    unlink_file,
)
from .manifest import File

log = logging.getLogger("rkai.storage")
CHUNK_BYTES = 256 * 1024
TRANSFER_ATTEMPTS = 3
MAX_TRANSFER_METADATA_BYTES = 16 * 1024


def fsync_dir(path: Path):
    with directory_fd(path) as fd:
        os.fsync(fd)


def atomic_write(path: Path, raw: bytes):
    tmp = path.with_name(path.name + ".tmp")
    with open_file(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    replace_file(tmp, path)


def verify(path: Path, spec: File) -> bool:
    try:
        with open_file(path) as f:
            if os.fstat(f.fileno()).st_size != spec.size:
                return False
            return hashlib.file_digest(f, "sha256").hexdigest() == spec.sha256
    except FileNotFoundError:
        return False


def download(
    http: httpx.Client,
    urls: list[str],
    destination: Path,
    spec: File,
    transfer_dir: Path | None = None,
):
    if verify(destination, spec):
        return
    transfer_base = destination
    if transfer_dir is not None:
        ensure_directory(transfer_dir)
        transfer_base = transfer_dir / hashlib.sha256(str(destination).encode()).hexdigest()
    partial = transfer_base.with_name(transfer_base.name + ".partial")
    meta = transfer_base.with_name(transfer_base.name + ".transfer.json")
    ensure_directory(destination.parent)
    last = None
    for endpoint_index, url in enumerate(urls):
        for attempt in range(TRANSFER_ATTEMPTS):
            try:
                try:
                    state = json.loads(read_file(meta, MAX_TRANSFER_METADATA_BYTES))
                except (FileNotFoundError, ValueError, RecursionError):
                    state = {}
                if not isinstance(state, dict):
                    state = {}
                offset = file_size(partial) if partial.exists() else 0
                if offset == spec.size and verify(partial, spec):
                    replace_file(partial, destination)
                    fsync_dir(destination.parent)
                    unlink_file(meta, missing_ok=True)
                    return
                if offset >= spec.size:
                    # A crash can leave a complete but unverified/corrupt partial.
                    offset = 0
                etag = state.get("etag")
                if (
                    state.get("url") != url
                    or not isinstance(etag, str)
                    or not etag
                    or etag.startswith("W/")
                ):
                    offset = 0
                headers = {"Range": f"bytes={offset}-", "If-Range": etag} if offset else {}
                with http.stream("GET", url, headers=headers) as response:
                    if response.status_code == 416 and offset:
                        unlink_file(partial, missing_ok=True)
                        unlink_file(meta, missing_ok=True)
                        raise RkaiError("invalid_range", "Source rejected saved range; restarting")
                    response.raise_for_status()
                    if response.headers.get("Content-Encoding", "identity") != "identity":
                        raise RkaiError("invalid_transfer", "Encoded transfer cannot be resumed")
                    if offset and response.status_code == 206:
                        expected = f"bytes {offset}-{spec.size - 1}/{spec.size}"
                        if (
                            response.headers.get("Content-Range") != expected
                            or response.headers.get("ETag") != etag
                        ):
                            unlink_file(partial, missing_ok=True)
                            unlink_file(meta, missing_ok=True)
                            raise RkaiError("invalid_range", "Source changed transfer validator")
                    elif response.status_code == 200:
                        offset = 0
                    else:
                        raise RkaiError("invalid_range", "Unexpected range response")
                    atomic_write(
                        meta,
                        json.dumps({"url": url, "etag": response.headers.get("ETag")}).encode(),
                    )
                    with open_file(partial, "ab" if offset else "wb") as f:
                        try:
                            for chunk in response.iter_bytes(CHUNK_BYTES):
                                offset += len(chunk)
                                if offset > spec.size:
                                    raise RkaiError(
                                        "size_mismatch", "Source exceeded declared size"
                                    )
                                f.write(chunk)
                                f.flush()
                        finally:
                            f.flush()
                            os.fsync(f.fileno())
                if not verify(partial, spec):
                    raise RkaiError(
                        "digest_mismatch", "Downloaded file failed size/SHA256 validation"
                    )
                replace_file(partial, destination)
                fsync_dir(destination.parent)
                unlink_file(meta, missing_ok=True)
                return
            except (httpx.HTTPError, RkaiError) as e:
                if isinstance(e, RkaiError) and e.code == "unsafe_path":
                    raise
                last = e
                log.warning(
                    "transfer endpoint=%d attempt=%d error=%s",
                    endpoint_index,
                    attempt + 1,
                    e.code if isinstance(e, RkaiError) else type(e).__name__,
                )
                if isinstance(e, RkaiError) and e.code in ("digest_mismatch", "size_mismatch"):
                    unlink_file(partial, missing_ok=True)
                    unlink_file(meta, missing_ok=True)
                    break
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                    break
    if isinstance(last, RkaiError):
        raise last
    raise RkaiError("download_failed", "Download interrupted; resume the saved operation") from last
