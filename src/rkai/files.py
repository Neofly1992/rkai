"""POSIX file access anchored to directory descriptors, never following symlinks."""

import errno
import os
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path

from .errors import RkaiError

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def unsafe_path():
    return RkaiError("unsafe_path", "File access requires real directories and regular files", 2)


@contextmanager
def directory_fd(path: Path, *, create: bool = False):
    """Open each component relative to an owned descriptor, not a checked pathname."""
    path = path.absolute()
    fd = os.open(path.anchor, DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            if part in (".", ".."):
                raise unsafe_path()
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise unsafe_path() from e
                raise
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def ensure_directory(path: Path):
    with directory_fd(path, create=True):
        pass


@contextmanager
def open_file(path: Path, mode: str = "rb"):
    writable = mode in ("wb", "ab")
    if mode not in ("rb", "wb", "ab"):
        raise ValueError("Only binary read/write/append modes are supported")
    with directory_fd(path.parent, create=writable) as parent:
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= os.O_WRONLY | os.O_CREAT if writable else os.O_RDONLY
        if mode == "ab":
            flags |= os.O_APPEND
        try:
            fd = os.open(path.name, flags, 0o600, dir_fd=parent)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise unsafe_path() from e
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (writable and info.st_nlink != 1):
                raise unsafe_path()
            # Truncate only after no-follow open and ownership checks, never while opening.
            if mode == "wb":
                os.ftruncate(fd, 0)
            stream = os.fdopen(fd, mode)
        except BaseException:
            os.close(fd)
            raise
        with stream:
            yield stream


def read_file(path: Path, limit: int | None = None) -> bytes:
    with open_file(path) as stream:
        return stream.read() if limit is None else stream.read(limit + 1)


def file_size(path: Path) -> int:
    with open_file(path) as stream:
        return os.fstat(stream.fileno()).st_size


def replace_file(source: Path, destination: Path):
    with directory_fd(source.parent) as src, directory_fd(destination.parent) as dst:
        try:
            info = os.stat(destination.name, dir_fd=dst, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode):
                raise unsafe_path()
        os.replace(source.name, destination.name, src_dir_fd=src, dst_dir_fd=dst)
        os.fsync(dst)


def unlink_file(path: Path, *, missing_ok: bool = False):
    with directory_fd(path.parent) as parent:
        try:
            os.unlink(path.name, dir_fd=parent)
        except FileNotFoundError:
            if not missing_ok:
                raise


def link_file(source: Path, destination: Path):
    with directory_fd(source.parent) as src, directory_fd(destination.parent) as dst:
        os.link(
            source.name, destination.name, src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False
        )


def move_directory(source: Path, destination: Path):
    with directory_fd(source.parent) as src, directory_fd(destination.parent) as dst:
        if not stat.S_ISDIR(os.stat(source.name, dir_fd=src, follow_symlinks=False).st_mode):
            raise unsafe_path()
        os.rename(source.name, destination.name, src_dir_fd=src, dst_dir_fd=dst)
        os.fsync(src)
        os.fsync(dst)


def remove_tree(path: Path):
    # The fd-based implementation refuses symlink roots and never traverses swapped links.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RkaiError(
            "unsupported_platform", "Safe directory removal requires POSIX file descriptors", 3
        )
    with directory_fd(path.parent) as parent:
        if not stat.S_ISDIR(os.stat(path.name, dir_fd=parent, follow_symlinks=False).st_mode):
            raise unsafe_path()
        shutil.rmtree(path.name, dir_fd=parent)
        os.fsync(parent)
