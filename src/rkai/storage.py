"""SQLite metadata and crash-safe file commit. All library mutation lives here."""

import base64
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import Config
from .download import CHUNK_BYTES, atomic_write, download, fsync_dir, verify
from .errors import RkaiError
from .files import (
    ensure_directory,
    link_file,
    move_directory,
    open_file,
    read_file,
    remove_tree,
    unlink_file,
)
from .manifest import MAX_MANIFEST_BYTES, digest, parse
from .sources import Provider, artifact_urls, client, resolve_reference

log = logging.getLogger("rkai.storage")
IDENTIFIER = re.compile(r"^[0-9a-f]{32,64}$")


@contextmanager
def lock(path: Path, shared: bool = False):
    """Fail-fast bounded admission. Lock files persist to keep inode identity."""
    with open_file(path, "ab") as f:
        try:
            fcntl.flock(f, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RkaiError("busy", "Resource is owned by another operation/session", 4) from e
        # Closing the final descriptor releases flock. An explicit LOCK_UN would
        # also unlock inherited guardian descriptors before native cleanup finishes.
        yield f.fileno()


class Library:
    def __init__(self, config: Config, http=None, fault=None):
        self.config = config
        self.root = Path(config.data_dir).expanduser().resolve()
        self.http = http
        self.fault = fault or (lambda stage: None)
        for name in ("staging", "installations", "blobs", "trash", "locks"):
            managed = self.root / name
            if managed.is_symlink():
                raise RkaiError("unsafe_path", "Managed directory is a symlink", 2)
            managed.mkdir(parents=True, exist_ok=True)
        # SQLite owns its opens; reject pre-existing linked database/sidecar files.
        # This library is private to the user, not a boundary against same-UID mutation.
        database = self.root / "library.sqlite3"
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = database.with_name(database.name + suffix)
            if suffix == "" or candidate.exists() or candidate.is_symlink():
                with open_file(candidate, "ab"):
                    pass
        self.db = sqlite3.connect(database, timeout=0)
        self.db.row_factory = sqlite3.Row
        try:
            with lock(self.root / "locks/library"):
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise RkaiError(
                        "unsupported_database", "Unsupported library schema; data preserved", 3
                    )
                if (
                    version == 0
                    and self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone()
                ):
                    raise RkaiError(
                        "unsupported_database", "Unrecognized database; data preserved", 3
                    )
                self.db.executescript("""
                PRAGMA foreign_keys=ON;
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS installations(
                    id TEXT PRIMARY KEY, model TEXT NOT NULL, revision TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL, plan_id TEXT NOT NULL,
                    snapshot TEXT NOT NULL, file_state TEXT NOT NULL DEFAULT 'ready',
                    execution_validation TEXT NOT NULL DEFAULT 'unverified',
                    UNIQUE(model, revision, manifest_hash, plan_id));
                CREATE TABLE IF NOT EXISTS aliases(
                    name TEXT PRIMARY KEY, installation TEXT NOT NULL REFERENCES installations(id));
                CREATE TABLE IF NOT EXISTS artifacts(
                    installation TEXT REFERENCES installations(id) ON DELETE CASCADE,
                    digest TEXT NOT NULL, PRIMARY KEY(installation,digest));
                CREATE TABLE IF NOT EXISTS operations(
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, plan TEXT NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS deletions(id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS execution_evidence(
                    installation TEXT REFERENCES installations(id) ON DELETE CASCADE,
                    session TEXT PRIMARY KEY, evidence TEXT NOT NULL);
                PRAGMA user_version=2;
            """)
        except BaseException:
            self.db.close()
            raise
        try:
            self.recover()
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def write(self):
        with lock(self.root / "locks/library"):
            try:
                self.db.execute("BEGIN IMMEDIATE")
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def state(self, operation: str, state: str, error: str | None = None):
        with self.write():
            self.db.execute(
                "UPDATE operations SET state=?,error=? WHERE id=?", (state, error, operation)
            )
        log.info("operation_id=%s state=%s", operation, state)

    def pull(self, ref: str, registry=None, target=None, plan_id=None, alias=None):
        if ref.startswith(("/", "./", "../", "file://")):
            if registry:
                raise RkaiError(
                    "source_conflict", "Local import and --registry are mutually exclusive", 2
                )
            if ref.startswith("file://"):
                uri = urlsplit(ref)
                if uri.netloc or uri.query or uri.fragment or not uri.path.startswith("/"):
                    raise RkaiError(
                        "invalid_reference", "file URI must have an absolute local path", 2
                    )
                ref = unquote(uri.path)
            path = Path(ref).expanduser().absolute()
            # The explicitly chosen import root may use OS aliases such as macOS /tmp.
            # Resolve that boundary once; reject links in the manifest and relative artifacts.
            path = path.parent.resolve() / path.name
            if path.is_symlink():
                raise RkaiError("unsafe_path", "Local manifest cannot be a symlink", 2)
            with open_file(path) as source:
                raw = source.read(MAX_MANIFEST_BYTES + 1)
            snapshot = {"kind": "local", "manifest_hash": digest(raw)}
            local_root = str(path.parent)
        else:
            reference = resolve_reference(ref, registry, self.config)
            with client() if self.http is None else _borrow(self.http) as http:
                raw, snapshot = Provider(http, self.config.registries[reference.registry]).manifest(
                    reference
                )
            local_root = None
        return self.acquire_manifest(raw, snapshot, local_root, target, plan_id, alias)

    def acquire_manifest(
        self, raw: bytes, snapshot: dict, local_root=None, target=None, plan_id=None, alias=None
    ):
        """One installation authority for published, converted and raw input snapshots."""
        snapshot = dict(snapshot)
        manifest = parse(raw)
        plan = manifest.select(plan_id, target)
        alias = alias or manifest.model.id
        if not alias or any(ord(c) < 32 for c in alias) or len(alias) > 256:
            raise RkaiError(
                "invalid_alias", "Alias must be nonempty printable text up to 256 chars", 2
            )
        snapshot["manifest_hash"] = digest(raw)
        if snapshot["kind"] == "https":
            snapshot["revision"] = digest(raw)
        identity = digest((digest(raw) + ":" + plan.id).encode())
        operation = uuid.uuid4().hex
        files = {}
        for name in sorted(manifest.closure(plan)):
            spec = manifest.files[name]
            if spec.location:
                urls = artifact_urls(
                    manifest.sources[spec.location.source],
                    spec.location.path,
                    self.config,
                    snapshot.get("registry", self.config.default_registry),
                )
                files[name] = {"urls": urls}
            elif local_root:
                files[name] = {"local": spec.path}
            else:
                raise RkaiError("missing_location", "Remote plan file has no download location", 2)
        saved = {
            "raw": base64.b64encode(raw).decode(),
            "plan_id": plan.id,
            "installation": identity,
            "alias": alias,
            "snapshot": snapshot,
            "local_root": local_root,
            "files": files,
        }
        with (
            lock(self.root / f"locks/operation-{operation}"),
            lock(self.root / f"locks/installation-{identity}", shared=True),
            self.write(),
        ):
            conflict = self.db.execute(
                "SELECT 1 FROM installations WHERE model=? AND revision=? AND manifest_hash!=?",
                (manifest.model.id, manifest.model.revision, digest(raw)),
            ).fetchone()
            if conflict:
                raise RkaiError(
                    "revision_conflict", "Published model revision changed its manifest", 2
                )
            self.db.execute(
                "INSERT INTO operations VALUES(?,?,?,NULL)",
                (operation, "planned", json.dumps(saved)),
            )
        log.info("operation_id=%s state=planned", operation)
        return self.resume(operation)

    def discard(self, operation: str) -> dict:
        if not IDENTIFIER.fullmatch(operation):
            raise RkaiError("invalid_operation", "Invalid operation id", 2)
        with lock(self.root / f"locks/operation-{operation}"):
            with self.write():
                row = self.db.execute(
                    "SELECT state,plan FROM operations WHERE id=?", (operation,)
                ).fetchone()
                if not row:
                    raise RkaiError("unknown_operation", "Operation does not exist", 2)
                if row["state"] == "completed":
                    raise RkaiError(
                        "completed_operation",
                        "Delete aliases to remove a completed installation",
                        2,
                    )
                saved = json.loads(row["plan"])
                self.db.execute(
                    "UPDATE operations SET state='cancelled',error=NULL WHERE id=?", (operation,)
                )
                installed = self.db.execute(
                    "SELECT 1 FROM installations WHERE id=?", (saved["installation"],)
                ).fetchone()
                if not installed:
                    self.db.execute(
                        "INSERT OR IGNORE INTO deletions VALUES(?)", (saved["installation"],)
                    )
            workspace = self.root / "staging" / operation
            if workspace.exists():
                remove_tree(workspace)
            self._recover_deletions()
        return {"operation_id": operation, "state": "cancelled", "aliases_changed": False}

    def resume(self, operation: str, *, recover_only: bool = False):
        if not IDENTIFIER.fullmatch(operation):
            raise RkaiError("invalid_operation", "Invalid operation id", 2)
        with lock(self.root / f"locks/operation-{operation}"):
            row = self.db.execute("SELECT * FROM operations WHERE id=?", (operation,)).fetchone()
            if not row:
                raise RkaiError("unknown_operation", "Operation does not exist", 2)
            saved = json.loads(row["plan"])
            if row["state"] == "completed":
                if not self.db.execute(
                    "SELECT 1 FROM installations WHERE id=?", (saved["installation"],)
                ).fetchone():
                    raise RkaiError(
                        "installation_removed", "Completed installation was subsequently deleted", 2
                    )
                manifest = parse(base64.b64decode(saved["raw"]))
                with lock(self.root / f"locks/installation-{saved['installation']}", shared=True):
                    self._verify_directory(
                        self.root / "installations" / saved["installation"], manifest, saved
                    )
                return self.result(operation, saved)
            if row["state"] == "cancelled":
                raise RkaiError("operation_cancelled", "Operation was explicitly discarded", 2)
            try:
                started = time.monotonic()
                result = self._install(operation, saved, recover_only=recover_only)
                try:
                    self._recover_deletions()
                except (OSError, RkaiError):
                    log.warning("Deferred retired installation cleanup")
                log.debug(
                    "operation_id=%s elapsed_seconds=%.3f", operation, time.monotonic() - started
                )
                return result
            except KeyboardInterrupt as e:
                self.state(operation, "paused", "interrupted")
                raise RkaiError("interrupted", f"Paused; resume operation {operation}", 130) from e
            except (RkaiError, OSError) as e:
                code = e.code if isinstance(e, RkaiError) else "storage_error"
                self.state(operation, "failed", code)
                raise RkaiError(
                    code,
                    f"{str(e) if isinstance(e, RkaiError) else 'Storage failed'}; "
                    f"resume operation {operation}",
                    e.exit_code if isinstance(e, RkaiError) else 1,
                ) from e

    def _install(self, operation: str, saved: dict, *, recover_only: bool):
        raw = base64.b64decode(saved["raw"])
        manifest = parse(raw)
        plan = manifest.select(saved["plan_id"], None)
        identity = saved["installation"]
        workspace = self.root / "staging" / operation
        stage = workspace / "tree"
        final = self.root / "installations" / identity
        with lock(self.root / f"locks/installation-{identity}", shared=True):
            if not final.exists():
                if recover_only:
                    # Startup/doctor recovery must not implicitly acquire missing network data.
                    self._verify_directory(stage, manifest, saved)
                ensure_directory(stage)
                remaining = sum(
                    manifest.files[n].size
                    for n in saved["files"]
                    if not verify(stage / manifest.files[n].path, manifest.files[n])
                )
                if shutil.disk_usage(self.root).free < remaining:
                    raise RkaiError("insufficient_space", "Insufficient free space for acquisition")
                self.state(operation, "downloading")
                with client() if self.http is None else _borrow(self.http) as http:
                    for name, location in saved["files"].items():
                        spec = manifest.files[name]
                        dest = stage / spec.path
                        if verify(dest, spec):
                            continue
                        ensure_directory(dest.parent)
                        blob = self.root / "blobs" / spec.sha256
                        if verify(blob, spec):
                            self._copy_file(blob, dest, spec.size)
                        elif "local" in location:
                            self._copy_file(
                                Path(saved["local_root"]) / location["local"], dest, spec.size
                            )
                        else:
                            download(http, location["urls"], dest, spec, workspace / "transfers")
                        if not verify(dest, spec):
                            unlink_file(dest, missing_ok=True)
                            raise RkaiError(
                                "digest_mismatch", "Imported file failed size/SHA256 validation"
                            )
                self.state(operation, "verifying")
                atomic_write(stage / "manifest.json", raw)
                self._verify_directory(stage, manifest, saved)
                # Sync nested directories and files before the atomic directory move.
                for directory, _, names in os.walk(stage, topdown=False):
                    for name in names:
                        with open_file(Path(directory) / name) as f:
                            os.fsync(f.fileno())
                    fsync_dir(Path(directory))
            return self._commit(operation, saved, manifest, plan, stage, final, workspace)

    def _commit(self, operation, saved, manifest, plan, stage, final, workspace):
        identity = saved["installation"]
        raw = base64.b64decode(saved["raw"])
        with defer_interrupts() as cancellation:
            self.state(operation, "committing")
            if not final.exists():
                self.fault("before_rename")
                with self.write():
                    if not final.exists():
                        move_directory(stage, final)
                self.fault("after_rename")
            self._verify_directory(final, manifest, saved)
            self._prepare_blobs(final, manifest, saved)
            with self.write():
                conflict = self.db.execute(
                    "SELECT 1 FROM installations WHERE model=? AND revision=? AND manifest_hash!=?",
                    (manifest.model.id, manifest.model.revision, digest(raw)),
                ).fetchone()
                if conflict:
                    raise RkaiError(
                        "revision_conflict", "Published model revision changed its manifest", 2
                    )
                self.db.execute(
                    "INSERT OR IGNORE INTO installations"
                    "(id,model,revision,manifest_hash,plan_id,snapshot) VALUES(?,?,?,?,?,?)",
                    (
                        identity,
                        manifest.model.id,
                        manifest.model.revision,
                        digest(raw),
                        plan.id,
                        json.dumps(saved["snapshot"]),
                    ),
                )
                for name in saved["files"]:
                    spec = manifest.files[name]
                    self.db.execute(
                        "INSERT OR IGNORE INTO artifacts VALUES(?,?)", (identity, spec.sha256)
                    )
                self.fault("before_db_commit")
                previous = self.db.execute(
                    "SELECT installation FROM aliases WHERE name=?", (saved["alias"],)
                ).fetchone()
                self.db.execute(
                    "INSERT INTO aliases VALUES(?,?) ON CONFLICT(name) DO UPDATE SET "
                    "installation=excluded.installation",
                    (saved["alias"], identity),
                )
                self.db.execute(
                    "UPDATE operations SET state='completed',error=NULL WHERE id=?", (operation,)
                )
                if previous and previous[0] != saved["installation"]:
                    if not self.db.execute(
                        "SELECT 1 FROM aliases WHERE installation=?", (previous[0],)
                    ).fetchone():
                        self.db.execute("INSERT OR IGNORE INTO deletions VALUES(?)", (previous[0],))
            if workspace.exists():
                try:
                    remove_tree(workspace)
                except (OSError, RkaiError):
                    log.warning("operation_id=%s staging cleanup deferred", operation)
            result = self.result(operation, saved)
            if cancellation["requested"]:
                result["cancellation_deferred"] = True
            return result

    def _copy_file(self, source: Path, destination: Path, size: int):
        with open_file(source) as src, open_file(destination, "wb") as dst:
            if os.fstat(src.fileno()).st_size != size:
                raise RkaiError("size_mismatch", "Local file differs from declared size")
            remaining = size
            while chunk := src.read(min(CHUNK_BYTES, remaining + 1)):
                remaining -= len(chunk)
                if remaining < 0:
                    raise RkaiError("size_mismatch", "Local file exceeded declared size")
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())

    def _prepare_blobs(self, final, manifest, saved):
        # The persisted pending plan protects these digests from GC until artifacts and
        # completed are committed together. A per-digest lock serializes CAS repair.
        prepared = set()
        for name in saved["files"]:
            spec = manifest.files[name]
            if spec.sha256 in prepared:
                continue
            with lock(self.root / f"locks/blob-{spec.sha256}"):
                blob = self.root / "blobs" / spec.sha256
                if not verify(blob, spec):
                    unlink_file(blob, missing_ok=True)
                    link_file(final / spec.path, blob)
                prepared.add(spec.sha256)
        fsync_dir(self.root / "blobs")

    def _verify_directory(self, directory, manifest, saved):
        try:
            raw = read_file(directory / "manifest.json", MAX_MANIFEST_BYTES)
            if digest(raw) != saved["snapshot"]["manifest_hash"]:
                raise RkaiError(
                    "corrupt_installation", "Committed manifest differs from saved plan"
                )
            for name in saved["files"]:
                spec = manifest.files[name]
                if not verify(directory / spec.path, spec):
                    raise RkaiError("corrupt_installation", "Installation file failed verification")
        except RkaiError as e:
            if e.code == "unsafe_path":
                raise RkaiError("corrupt_installation", "Installation file path is unsafe") from e
            raise

    def inspect(self, alias: str) -> dict:
        with self.lease(alias) as identity:
            row = self.db.execute("SELECT * FROM installations WHERE id=?", (identity,)).fetchone()
            directory = self.root / "installations" / identity
            state = "ready"
            try:
                raw = read_file(directory / "manifest.json", MAX_MANIFEST_BYTES)
                manifest = parse(raw)
                plan = manifest.select(row["plan_id"], None)
                self._verify_directory(
                    directory,
                    manifest,
                    {
                        "snapshot": {"manifest_hash": row["manifest_hash"]},
                        "files": manifest.closure(plan),
                    },
                )
            except FileNotFoundError:
                state = "missing"
            except (OSError, RkaiError):
                state = "corrupt"
            with self.write():
                self.db.execute(
                    "UPDATE installations SET file_state=? WHERE id=?", (state, identity)
                )
            return {"file_state": state, "execution_validation": row["execution_validation"]}

    def models(self):
        """Installed aliases with verified files; readiness is separate from execution evidence."""
        result = []
        for row in self.db.execute(
            "SELECT a.name,i.id,i.plan_id FROM aliases a JOIN installations i "
            "ON a.installation=i.id ORDER BY a.name"
        ).fetchall():
            state = self.inspect(row["name"])
            if state["file_state"] != "ready":
                continue
            with self.lease(row["name"]) as identity:
                if identity != row["id"]:
                    raise RkaiError("busy", "Alias changed during model listing", 4)
                manifest = parse(
                    read_file(
                        self.root / "installations" / identity / "manifest.json", MAX_MANIFEST_BYTES
                    )
                )
                plan = manifest.select(row["plan_id"], None)
                result.append(
                    {
                        "id": row["name"],
                        "installation": identity,
                        "capabilities": [c.id for c in plan.capabilities],
                        **state,
                    }
                )
        return result

    def result(self, operation, saved):
        return {
            "operation_id": operation,
            "state": "completed",
            "alias": saved["alias"],
            "installation": saved["installation"],
            "snapshot": saved["snapshot"],
            "file_state": "ready",
            "execution_validation": "unverified",
        }

    def record_execution(self, identity, session, evidence):
        """Actual execution evidence is scoped to device/runtime, never file readiness."""
        with self.write():
            self.db.execute(
                "INSERT OR REPLACE INTO execution_evidence VALUES(?,?,?)",
                (identity, session, json.dumps(evidence)),
            )
            if evidence["device"] == "rockchip-npu":
                self.db.execute(
                    "UPDATE installations SET execution_validation='passed' WHERE id=?", (identity,)
                )

    @contextmanager
    def lease(self, alias: str, retain_fds: list[int] | None = None):
        row = self.db.execute("SELECT installation FROM aliases WHERE name=?", (alias,)).fetchone()
        if not row:
            raise RkaiError("unknown_alias", "Alias is not installed", 2)
        identity = row[0]
        with lock(self.root / f"locks/installation-{identity}", shared=True) as descriptor:
            if retain_fds is not None:
                retain_fds.append(descriptor)
            check = self.db.execute(
                "SELECT installation FROM aliases WHERE name=?", (alias,)
            ).fetchone()
            if not check or check[0] != identity:
                raise RkaiError("busy", "Alias changed while acquiring lease", 4)
            yield identity

    def delete(self, alias: str, purge: bool = False):
        row = self.db.execute("SELECT installation FROM aliases WHERE name=?", (alias,)).fetchone()
        if not row:
            raise RkaiError("unknown_alias", "Alias is not installed", 2)
        identity = row[0]
        with lock(self.root / f"locks/installation-{identity}"):
            with self.write():
                current = self.db.execute(
                    "SELECT installation FROM aliases WHERE name=?", (alias,)
                ).fetchone()
                if not current or current[0] != identity:
                    raise RkaiError("busy", "Alias changed during deletion", 4)
                for op in self.db.execute(
                    "SELECT plan FROM operations WHERE state NOT IN ('completed','cancelled')"
                ):
                    if json.loads(op[0])["installation"] == identity:
                        raise RkaiError(
                            "busy", "Installation is referenced by a pending operation", 4
                        )
                affected = (
                    [
                        r[0]
                        for r in self.db.execute(
                            "SELECT name FROM aliases WHERE installation=?", (identity,)
                        )
                    ]
                    if purge
                    else [alias]
                )
                for name in affected:
                    self.db.execute("DELETE FROM aliases WHERE name=?", (name,))
                remaining = self.db.execute(
                    "SELECT 1 FROM aliases WHERE installation=?", (identity,)
                ).fetchone()
                if not remaining:
                    self.db.execute("INSERT OR IGNORE INTO deletions VALUES(?)", (identity,))
            if not remaining:
                self.fault("delete_marked")
                self._finish_delete(identity)
        return {"deleted_aliases": affected, "removed_installation": not bool(remaining)}

    def _finish_delete(self, identity):
        with self.write():
            if self.db.execute(
                "SELECT 1 FROM aliases WHERE installation=?", (identity,)
            ).fetchone():
                self.db.execute("DELETE FROM deletions WHERE id=?", (identity,))
                return
            for row in self.db.execute(
                "SELECT plan FROM operations WHERE state NOT IN ('completed','cancelled')"
            ):
                if json.loads(row[0])["installation"] == identity:
                    return
            final = self.root / "installations" / identity
            trash = self.root / "trash" / identity
            if final.exists():
                move_directory(final, trash)
            self.fault("delete_moved")
            self.db.execute("DELETE FROM installations WHERE id=?", (identity,))
        if trash.exists():
            remove_tree(trash)
        with self.write():
            referenced = {r[0] for r in self.db.execute("SELECT digest FROM artifacts")}
            for row in self.db.execute(
                "SELECT plan FROM operations WHERE state NOT IN ('completed','cancelled')"
            ):
                saved = json.loads(row[0])
                m = parse(base64.b64decode(saved["raw"]))
                referenced.update(m.files[n].sha256 for n in saved["files"])
            for blob in (self.root / "blobs").iterdir():
                if blob.name not in referenced:
                    unlink_file(blob)
            fsync_dir(self.root / "blobs")
            self.db.execute("DELETE FROM deletions WHERE id=?", (identity,))

    def _recover_deletions(self):
        for row in self.db.execute("SELECT id FROM deletions").fetchall():
            try:
                with lock(self.root / f"locks/installation-{row[0]}"):
                    self._finish_delete(row[0])
            except RkaiError as e:
                if e.code != "busy":
                    raise

    def recover(self):
        self._recover_deletions()
        for row in self.db.execute("SELECT id FROM operations WHERE state='committing'").fetchall():
            try:
                self.resume(row[0], recover_only=True)
            except RkaiError as e:
                if e.code != "busy":
                    log.warning("recovery operation_id=%s error=%s", row[0], e.code)
        from .conversion import recover as recover_conversions

        recover_conversions(self)


@contextmanager
def _borrow(http):
    yield http


@contextmanager
def defer_interrupts():
    """Once committing, SIGINT requests finish consistency and return an explicit result."""
    state = {"requested": False}
    if threading.current_thread() is not threading.main_thread():
        yield state
        return
    previous = signal.getsignal(signal.SIGINT)

    def defer(signum, frame):
        state["requested"] = True

    signal.signal(signal.SIGINT, defer)
    try:
        yield state
    finally:
        signal.signal(signal.SIGINT, previous)
