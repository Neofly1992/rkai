import json
import multiprocessing

import httpx
import pytest

from rkai.errors import RkaiError
from rkai.storage import Library, lock


class Crash(BaseException):
    pass


def crash_at(wanted):
    def crash(stage):
        if stage == wanted:
            raise Crash(stage)

    return crash


def test_local_copy_alias_sharing(config, bundle):
    path, _, content = bundle
    with Library(config) as library:
        first = library.pull(str(path), alias="one", target="rk3588")
        second = library.pull(str(path), alias="two", target="rk3588")
        assert first["installation"] == second["installation"]
        assert library.delete("one")["removed_installation"] is False
        assert len(list((library.root / "blobs").iterdir())) == 1
        with library.lease("two"), pytest.raises(RkaiError) as error:
            library.delete("two", purge=True)
        assert error.value.code == "busy"
        assert library.delete("two")["removed_installation"] is True
        assert not list((library.root / "blobs").iterdir())
    assert (path.parent / "payload.bin").read_bytes() == content


@pytest.mark.parametrize("stage", ["before_rename", "after_rename", "before_db_commit"])
def test_commit_crash_reopens_and_recovers(config, bundle, stage):
    with Library(config, fault=crash_at(stage)) as library, pytest.raises(Crash):
        library.pull(str(bundle[0]), alias="model", target="rk3588")
    with Library(config) as library:
        row = library.db.execute("SELECT * FROM operations").fetchone()
        assert row["state"] == "completed"
        with library.lease("model") as identity:
            assert (library.root / "installations" / identity / "payload.bin").is_file()
        assert library.resume(row["id"])["state"] == "completed"


@pytest.mark.parametrize("stage", ["delete_marked", "delete_moved"])
def test_delete_crash_recovery(config, bundle, stage):
    with Library(config) as library:
        library.pull(str(bundle[0]), alias="model", target="rk3588")
        library.fault = crash_at(stage)
        with pytest.raises(Crash):
            library.delete("model")
    with Library(config) as library:
        assert not library.db.execute("SELECT * FROM aliases").fetchall()
        assert not library.db.execute("SELECT * FROM installations").fetchall()
        assert not list((library.root / "trash").iterdir())
        assert not list((library.root / "blobs").iterdir())


def test_failed_replacement_preserves_alias(config, bundle):
    path, doc, _ = bundle
    with Library(config) as library:
        original = library.pull(str(path), alias="model", target="rk3588")
        doc["model"]["revision"] = "r2"
        doc["files"]["payload"]["sha256"] = "0" * 64
        path.write_text(json.dumps(doc))
        with pytest.raises(RkaiError) as error:
            library.pull(str(path), alias="model", target="rk3588")
        assert error.value.code == "digest_mismatch"
        with library.lease("model") as identity:
            assert identity == original["installation"]
        assert (
            library.db.execute("SELECT state FROM operations ORDER BY rowid DESC").fetchone()[0]
            == "failed"
        )


def test_revision_cannot_silently_change(config, bundle):
    path, doc, _ = bundle
    with Library(config) as library:
        library.pull(str(path), target="rk3588")
        doc["model"]["description"] = "changed"
        path.write_text(json.dumps(doc))
        with pytest.raises(RkaiError) as error:
            library.pull(str(path), target="rk3588")
        assert error.value.code == "revision_conflict"


def test_symlink_import_rejected(config, bundle, tmp_path):
    path, _, _ = bundle
    payload = path.parent / "payload.bin"
    target = tmp_path / "outside"
    payload.rename(target)
    payload.symlink_to(target)
    with Library(config) as library, pytest.raises(RkaiError) as error:
        library.pull(str(path), target="rk3588")
    assert error.value.code == "unsafe_path"
    assert target.exists()


def test_shared_blob_survives_other_installation_delete(config, bundle):
    path, doc, _ = bundle
    with Library(config) as library:
        first = library.pull(str(path), alias="first", target="rk3588")
        doc["model"]["revision"] = "r2"
        path.write_text(json.dumps(doc))
        second = library.pull(str(path), alias="second", target="rk3588")
        assert first["installation"] != second["installation"]
        library.delete("first")
        assert len(list((library.root / "blobs").iterdir())) == 1
        with library.lease("second") as identity:
            assert (library.root / "installations" / identity / "payload.bin").exists()


def test_pending_operation_delete_protection(config, bundle):
    path = bundle[0]
    with Library(config) as library:
        library.pull(str(path), alias="one", target="rk3588")
        library.fault = crash_at("before_db_commit")
        with pytest.raises(Crash):
            library.pull(str(path), alias="two", target="rk3588")
        with pytest.raises(RkaiError) as error:
            library.delete("one", purge=True)
        assert error.value.code == "busy"


def test_fixed_latest_resume_does_not_resolve_again(config, bundle):
    _, doc, content = bundle
    doc["sources"] = {
        "s": {
            "kind": "huggingface",
            "origin": "https://huggingface.co",
            "repository": "org/model",
            "revision": "a" * 40,
        }
    }
    doc["files"]["payload"]["location"] = {"source": "s", "path": "payload.bin"}
    requests = []
    interrupt = True

    def handler(request):
        nonlocal interrupt
        requests.append(str(request.url))
        if "/api/models/" in request.url.path:
            return httpx.Response(200, json={"sha": "a" * 40})
        if request.url.path.endswith("manifest.json"):
            return httpx.Response(200, json=doc)
        if interrupt:
            interrupt = False
            raise KeyboardInterrupt
        return httpx.Response(200, content=content)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        Library(config, http=http) as lib,
    ):
        with pytest.raises(RkaiError) as error:
            lib.pull("hf://org/model@latest", alias="m", target="rk3588")
        assert error.value.exit_code == 130
        row = lib.db.execute("SELECT * FROM operations").fetchone()
        assert row["state"] == "paused"
        before = len(requests)
        result = lib.resume(row["id"])
        assert result["snapshot"]["selector"] == "latest"
        assert result["snapshot"]["revision"] == "a" * 40
        assert len(requests) == before + 1
        assert "/resolve/" + "a" * 40 in requests[-1]


def _hold_lock(path, pipe):
    with lock(path):
        pipe.send("ready")
        pipe.recv()


def test_cross_process_operation_lock(config, bundle):
    from pathlib import Path

    path = Path(config.data_dir) / "locks/operation-test"
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_hold_lock, args=(path, child))
    process.start()
    try:
        assert parent.poll(10)
        assert parent.recv() == "ready"
        with pytest.raises(RkaiError) as e:
            with lock(path):
                pass
        assert e.value.code == "busy"
    finally:
        parent.send("exit")
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join()
    assert process.exitcode == 0


def test_sigint_during_commit_returns_explicit_installation(config, bundle):
    import os
    import signal

    def interrupt(stage):
        if stage == "after_rename":
            os.kill(os.getpid(), signal.SIGINT)

    with Library(config, fault=interrupt) as library:
        result = library.pull(str(bundle[0]), alias="m", target="rk3588")
        assert result["cancellation_deferred"] is True
        assert library.db.execute("SELECT state FROM operations").fetchone()[0] == "completed"
        with library.lease("m"):
            pass


def test_alias_update_retires_old_installation_after_lease(config, bundle):
    path, doc, _ = bundle
    with Library(config) as library:
        original = library.pull(str(path), alias="m", target="rk3588")
        with library.lease("m"):
            doc["model"]["revision"] = "r2"
            path.write_text(json.dumps(doc))
            replacement = library.pull(str(path), alias="m", target="rk3588")
            assert original["installation"] != replacement["installation"]
            assert (library.root / "installations" / original["installation"]).exists()
        library.recover()
        assert not (library.root / "installations" / original["installation"]).exists()
        assert len(list((library.root / "blobs").iterdir())) == 1


def test_future_database_preserved(config):
    import sqlite3
    from pathlib import Path

    root = Path(config.data_dir)
    root.mkdir()
    with sqlite3.connect(root / "library.sqlite3") as db:
        db.execute("CREATE TABLE private_data(value TEXT)")
        db.execute("INSERT INTO private_data VALUES('preserve')")
        db.execute("PRAGMA user_version=42")
    with pytest.raises(RkaiError) as exc:
        Library(config)
    assert exc.value.code == "unsupported_database"
    with sqlite3.connect(root / "library.sqlite3") as db:
        assert db.execute("SELECT value FROM private_data").fetchone()[0] == "preserve"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 42


def test_manifest_corruption_reported_by_inspection(config, bundle):
    with Library(config) as library:
        result = library.pull(str(bundle[0]), alias="m", target="rk3588")
        path = library.root / "installations" / result["installation"] / "manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
        assert library.inspect("m")["file_state"] == "corrupt"
        assert library.db.execute("SELECT file_state FROM installations").fetchone()[0] == "corrupt"


def test_artifact_names_cannot_collide_with_transfer_state(config, bundle):
    path, doc, content = bundle
    doc["sources"] = {"s": {"kind": "https", "base_url": "https://fixture.example"}}
    for name in ("payload.bin.partial", "payload.bin.transfer.json", "manifest.json.tmp"):
        doc["files"][name] = {
            "path": name,
            "size": len(content),
            "sha256": doc["files"]["payload"]["sha256"],
            "location": {"source": "s", "path": name},
        }
        doc["plans"][0]["components"][0]["files"][name] = name
    # manifest.json.tmp conflicts with atomic_write, so it must be reserved too.
    path.write_text(json.dumps(doc))
    with Library(config) as library, pytest.raises(RkaiError) as exc:
        library.pull(str(path), target="rk3588")
    assert exc.value.code == "invalid_manifest"


def test_persisted_pause_reopens_with_same_plan(config, bundle):
    path, doc, content = bundle
    doc["sources"] = {"s": {"kind": "https", "base_url": "https://source.example"}}
    doc["files"]["payload"]["location"] = {"source": "s", "path": "data"}
    path.write_text(json.dumps(doc))
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(KeyboardInterrupt())
            )
        ) as http,
        Library(config, http=http) as library,
    ):
        with pytest.raises(RkaiError):
            library.pull(str(path), target="rk3588")
        row = library.db.execute("SELECT id,state FROM operations").fetchone()
        operation = row["id"]
        assert row["state"] == "paused"
    # External manifest may move/change; resume only needs saved bytes and snapshot URLs.
    path.unlink()
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))
        ) as http,
        Library(config, http=http) as library,
    ):
        result = library.resume(operation)
        assert result["state"] == "completed"


def test_operation_cannot_resurrect_deleted_installation(config, bundle):
    with Library(config) as library:
        result = library.pull(str(bundle[0]), alias="m", target="rk3588")
        library.delete("m")
        with pytest.raises(RkaiError) as error:
            library.resume(result["operation_id"])
        assert error.value.code == "installation_removed"


def test_parallel_install_operations_do_not_share_transfer_state(config, bundle):
    # Same content plans deduplicate installation; operations always own distinct staging.
    path, doc, content = bundle
    doc["sources"] = {"s": {"kind": "https", "base_url": "https://source.example"}}
    doc["files"]["payload"]["location"] = {"source": "s", "path": "data"}
    path.write_text(json.dumps(doc))
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(KeyboardInterrupt())
            )
        ) as http,
        Library(config, http=http) as library,
    ):
        for alias in ("one", "two"):
            with pytest.raises(RkaiError):
                library.pull(str(path), alias=alias, target="rk3588")
        rows = library.db.execute("SELECT id FROM operations").fetchall()
        assert len(rows) == 2 and rows[0][0] != rows[1][0]
        assert len(list((library.root / "staging").iterdir())) == 2
        library.http = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))
        )
        try:
            first = library.resume(rows[0][0])
            second = library.resume(rows[1][0])
        finally:
            library.http.close()
        assert first["installation"] == second["installation"]
        assert library.delete("one")["removed_installation"] is False
        assert library.delete("two")["removed_installation"] is True


def test_discard_releases_preparation_protection_without_changing_aliases(config, bundle):
    path = bundle[0]
    with Library(config) as library:
        installed = library.pull(str(path), alias="one", target="rk3588")
        library.fault = crash_at("before_db_commit")
        with pytest.raises(Crash):
            library.pull(str(path), alias="two", target="rk3588")
        op = library.db.execute("SELECT id FROM operations WHERE state='committing'").fetchone()[0]
        with pytest.raises(RkaiError):
            library.delete("one")
        library.fault = lambda stage: None
        assert library.discard(op)["aliases_changed"] is False
        with library.lease("one") as identity:
            assert identity == installed["installation"]
        with pytest.raises(RkaiError) as error:
            library.resume(op)
        assert error.value.code == "operation_cancelled"
        assert library.delete("one")["removed_installation"] is True


def test_discard_unregistered_directory_after_commit_crash(config, bundle):
    # Deliberately discard before startup recovery owns the committing operation.
    with Library(config, fault=crash_at("after_rename")) as library:
        with pytest.raises(Crash):
            library.pull(str(bundle[0]), alias="m", target="rk3588")
        op = library.db.execute("SELECT id FROM operations").fetchone()[0]
        library.fault = lambda stage: None
        assert library.discard(op)["state"] == "cancelled"
        assert not list((library.root / "installations").iterdir())
        assert not library.db.execute("SELECT * FROM aliases").fetchall()


def test_startup_recovery_never_downloads_missing_committing_files(config, bundle):
    with Library(config, fault=crash_at("before_rename")) as library:
        with pytest.raises(Crash):
            library.pull(str(bundle[0]), target="rk3588")
        row = library.db.execute("SELECT id FROM operations").fetchone()
        (library.root / "staging" / row[0] / "tree/payload.bin").unlink()
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError("startup attempted network"))
            )
        ) as http,
        Library(config, http=http) as library,
    ):
        assert library.db.execute("SELECT state FROM operations").fetchone()[0] == "failed"
        assert not library.db.execute("SELECT * FROM aliases").fetchall()


def test_file_uri_with_encoded_spaces(config, bundle):
    path = bundle[0]
    new_root = path.parent.with_name("external with spaces")
    path.parent.rename(new_root)
    path = new_root / path.name
    with Library(config) as library:
        assert library.pull(path.as_uri(), target="rk3588")["file_state"] == "ready"
        with pytest.raises(RkaiError) as error:
            library.pull("file://remote-host/model.json", target="rk3588")
        assert error.value.code == "invalid_reference"


@pytest.mark.parametrize("source_kind", ["remote", "local", "blob"])
def test_resume_symlink_parent_preserves_external_file(config, bundle, tmp_path, source_kind):
    path, doc, content = bundle
    if source_kind == "blob":
        with Library(config) as seed:
            seed.pull(str(path), alias="seed", target="rk3588")
        doc["model"]["revision"] = "r2"
    doc["files"]["payload"]["path"] = "nested/payload.bin"
    (path.parent / "nested").mkdir()
    (path.parent / "nested/payload.bin").write_bytes(content)
    if source_kind == "remote":
        doc["sources"] = {"s": {"kind": "https", "base_url": "https://source.example"}}
        doc["files"]["payload"]["location"] = {"source": "s", "path": "data"}
    path.write_text(json.dumps(doc))

    # Pause before any data writes; then alter persisted staging as in the reported recovery bug.
    with Library(config) as library:
        original_state = library.state

        def pause(operation, state, error=None):
            if state == "downloading":
                raise KeyboardInterrupt
            original_state(operation, state, error)

        library.state = pause
        with pytest.raises(RkaiError):
            library.pull(str(path), alias="new", target="rk3588")
        operation = library.db.execute("SELECT id FROM operations WHERE state='paused'").fetchone()[
            0
        ]
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "payload.bin"
    victim.write_bytes(b"original external file")
    (tmp_path / "library/staging" / operation / "tree/nested").symlink_to(
        outside, target_is_directory=True
    )
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))
        ) as http,
        Library(config, http=http) as library,
    ):
        with pytest.raises(RkaiError) as error:
            library.resume(operation)
        assert error.value.code == "unsafe_path"
        assert library.db.execute("SELECT 1 FROM aliases WHERE name='new'").fetchone() is None
    assert victim.read_bytes() == b"original external file"


def test_blob_hashing_allows_unrelated_database_write(config, bundle, monkeypatch):
    import rkai.storage as storage

    with Library(config) as library:
        installed = library.pull(str(bundle[0]), alias="one", target="rk3588")
    checks = []
    original_verify = storage.verify
    with Library(config) as library, Library(config) as other:

        def concurrent_verify(path, spec):
            if path.parent == library.root / "blobs":
                assert not library.db.in_transaction
                # A separate connection can write while the blob is being hashed.
                with other.write():
                    other.db.execute(
                        "INSERT OR IGNORE INTO aliases VALUES(?,?)",
                        ("concurrent", installed["installation"]),
                    )
                checks.append(path)
            return original_verify(path, spec)

        monkeypatch.setattr(storage, "verify", concurrent_verify)
        library.pull(str(bundle[0]), alias="two", target="rk3588")
        assert checks
        assert library.db.execute("SELECT COUNT(*) FROM aliases").fetchone()[0] == 3


def test_pending_plan_protects_prepared_blobs_from_gc(config, bundle, monkeypatch):
    path, doc, _ = bundle
    with Library(config) as library:
        seed = library.pull(str(path), alias="seed", target="rk3588")
        doc["model"]["revision"] = "r2"
        path.write_text(json.dumps(doc))
        prepare = library._prepare_blobs

        def prepare_then_delete(final, manifest, saved):
            prepare(final, manifest, saved)
            library.delete("seed")
            # No artifact row references this blob now; the pending plan must protect it.
            assert not library.db.execute("SELECT 1 FROM artifacts").fetchone()
            assert list((library.root / "blobs").iterdir())

        monkeypatch.setattr(library, "_prepare_blobs", prepare_then_delete)
        result = library.pull(str(path), alias="new", target="rk3588")
        assert result["installation"] != seed["installation"]
        assert library.inspect("new")["file_state"] == "ready"


def test_download_parent_changed_after_request_never_overwrites_external(config, bundle, tmp_path):
    path, doc, content = bundle
    doc["files"]["payload"]["path"] = "nested/payload.bin"
    doc["sources"] = {"s": {"kind": "https", "base_url": "https://source.example"}}
    doc["files"]["payload"]["location"] = {"source": "s", "path": "data"}
    path.write_text(json.dumps(doc))
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "payload.bin"
    victim.write_bytes(b"original external file")
    visits = []
    with Library(config) as library:

        def route(request):
            visits.append(request)
            op = library.db.execute("SELECT id FROM operations").fetchone()[0]
            nested = library.root / "staging" / op / "tree/nested"
            nested.rmdir()
            nested.symlink_to(outside, target_is_directory=True)
            return httpx.Response(200, content=content)

        with httpx.Client(transport=httpx.MockTransport(route)) as http:
            library.http = http
            with pytest.raises(RkaiError) as error:
                library.pull(str(path), target="rk3588")
        assert error.value.code == "unsafe_path"
        assert len(visits) == 1
    assert victim.read_bytes() == b"original external file"


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_database_links_rejected_without_modifying_target(config, tmp_path, suffix, kind):
    from pathlib import Path

    root = Path(config.data_dir)
    root.mkdir()
    external = tmp_path / "external.sqlite3"
    external.write_bytes(b"do not modify")
    candidate = root / ("library.sqlite3" + suffix)
    if kind == "symlink":
        candidate.symlink_to(external)
    else:
        candidate.hardlink_to(external)
    with pytest.raises(RkaiError) as error:
        Library(config)
    assert error.value.code == "unsafe_path"
    assert external.read_bytes() == b"do not modify"


def test_import_rejects_oversize_before_copy(config, bundle):
    path, _, content = bundle
    path.with_name("payload.bin").write_bytes(content * 100)
    with Library(config) as library:
        with pytest.raises(RkaiError) as error:
            library.pull(str(path), target="rk3588")
        assert error.value.code == "size_mismatch"
        assert all(p.stat().st_size == 0 for p in library.root.glob("staging/*/tree/payload.bin"))
        assert library.db.execute("SELECT state FROM operations").fetchone()[0] == "failed"


def test_explicit_import_root_alias_is_normalized(config, bundle, tmp_path):
    path, _, _ = bundle
    linked = tmp_path / "linked"
    linked.symlink_to(path.parent, target_is_directory=True)
    with Library(config) as library:
        result = library.pull(str(linked / path.name), target="rk3588")
        assert result["file_state"] == "ready"


def test_inspect_symlink_manifest_reports_corrupt(config, bundle, tmp_path):
    path, _, _ = bundle
    with Library(config) as library:
        result = library.pull(str(path), target="rk3588")
        installed = library.root / "installations" / result["installation"] / "manifest.json"
        installed.unlink()
        installed.symlink_to(path)
        assert library.inspect(result["alias"])["file_state"] == "corrupt"
