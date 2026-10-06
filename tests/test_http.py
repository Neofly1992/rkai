"""Real TCP HTTP + SQLite + guardian/native ONNX. No direct Session substitutes."""

import concurrent.futures
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from test_execution import executable as executable_fixture
from test_execution import hanging_model, install

from rkai.errors import RkaiError
from rkai.http import Server, create_app
from rkai.storage import Library

executable = executable_fixture  # Shared real-worker fixture.


@contextmanager
def live(config):
    app = create_app(config)
    server = Server(uvicorn.Config(app, log_level="error", access_log=False))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
        thread.start()
        until = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > until:
                raise AssertionError("HTTP service failed startup")
            time.sleep(0.01)
        try:
            with httpx.Client(base_url=base, timeout=15, trust_env=False) as client:
                yield client, app.state.service
        finally:
            app.state.service.stop()
            server.should_exit = True
            thread.join(15)
            assert not thread.is_alive()
            assert app.state.service.admission.requests == 0
            assert app.state.service.admission.disk == 0
            assert app.state.service.admission.memory == 0


def upload(client, image, **fields):
    metadata = {
        "model": "vision",
        "capability": "classification",
        "input": {"image": "media:image"},
        **fields,
    }
    return client.post(
        "/api/v1/infer",
        files={
            "metadata": (None, json.dumps(metadata), "application/json"),
            "image": ("../../escape.png", image.read_bytes(), "image/png"),
        },
    )


def until_idle(service):
    until = time.monotonic() + 10
    while service.admission.requests:
        assert time.monotonic() < until
        time.sleep(0.01)


def test_http_classification_lazy_shared_load_switch_and_cli_lease(config, executable):
    config.allow_cpu_fallback = True
    with Library(config) as library:
        install(library, executable)
        library.pull(str(executable[0] / "manifest.json"), plan_id="cpu", alias="other")
    with live(config) as (client, service):
        health = client.get("/health").json()
        assert health["ready"] and health["model"]["state"] == "unloaded"
        assert len(client.get("/v1/models").json()["data"]) == 2
        result = upload(client, executable[1])
        assert result.status_code == 200, result.text
        assert result.json()["capability_result"]["labels"][0]["label"] == "red"
        original = service.session.id
        assert upload(client, executable[1]).status_code == 200
        assert service.session.id == original
        with Library(config) as library, pytest.raises(RkaiError) as busy:
            library.delete("vision")
        assert busy.value.code == "busy"
        assert upload(client, executable[1], model="other").status_code == 200
        assert service.session.id != original
        # Same shared installation is still leased after switching its alias.
        with Library(config) as library, pytest.raises(RkaiError):
            library.delete("vision")
        assert client.get("/v1/models").json()["data"][0]["loaded"]
    with Library(config) as library:
        library.delete("vision")
        library.delete("other")


def test_http_native_sse_exact_terminal(config, executable):
    config.allow_cpu_fallback = True
    with Library(config) as library:
        install(library, executable)
    with live(config) as (client, _):
        result = upload(client, executable[1], stream=True)
        assert result.status_code == 200
        events = [
            json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")
        ]
        assert [e["type"] for e in events] == ["accepted", "started", "output", "completed"]
        assert [e["sequence"] for e in events] == list(range(4))


@pytest.mark.parametrize(
    "path,data,code",
    [
        (
            "/v1/chat/completions",
            {
                "model": "missing",
                "messages": [{"role": "user", "content": "x"}],
                "max_completion_tokens": 0,
            },
            "invalid_input",
        ),
        (
            "/v1/chat/completions",
            {"model": "missing", "messages": [{"role": "user", "content": "x"}], "n": True},
            "invalid_input",
        ),
        (
            "/v1/chat/completions",
            {"model": "missing", "messages": [{"role": "user", "content": "x"}], "tools": []},
            "unsupported_parameter",
        ),
        (
            "/v1/messages",
            {
                "model": "missing",
                "messages": [{"role": "system", "content": "x"}],
                "max_tokens": 10,
            },
            "invalid_input",
        ),
        ("/v1/embeddings", {"model": "missing", "input": [1]}, "invalid_input"),
        ("/v1/embeddings", {"model": "missing", "input": "x"}, "model_not_found"),
        (
            "/api/v1/infer",
            {"model": "vision", "capability": "decision", "input": {}},
            "unsupported_capability",
        ),
        (
            "/api/v1/infer",
            {"model": "vision", "capability": "classification", "input": {"image": "/etc/passwd"}},
            "invalid_input",
        ),
        (
            "/v1/chat/completions",
            {
                "model": "missing",
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": True,
            },
            "invalid_input",
        ),
    ],
)
def test_http_protocol_rejections(config, path, data, code):
    with live(config) as (client, _):
        result = client.post(path, json=data)
        assert result.status_code >= 400
        assert result.json()["error"]["code"] == code


def test_http_unknown_length_json_and_multipart_limits(config, executable):
    config.http.limits.json_bytes = 30
    config.http.limits.media_file_bytes = 5
    with live(config) as (client, service):
        response = client.post(
            "/v1/embeddings",
            content=iter([b"{" + b"x" * 20, b"x" * 20]),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 413
        assert upload(client, executable[1]).status_code == 413
        assert service.admission.disk == 0
        config.http.limits.metadata_bytes = 10
        config.http.limits.media_file_bytes = 1000
        assert upload(client, executable[1]).status_code == 413
        assert not service.session


def test_http_decode_error_retires_and_removes_media(config, executable, tmp_path):
    config.allow_cpu_fallback = True
    with Library(config) as library:
        install(library, executable)
    bad = tmp_path / "bad"
    bad.write_bytes(b"not an image")
    with live(config) as (client, service):
        result = upload(client, bad)
        assert result.status_code == 400, result.text
        until_idle(service)
        assert service.session is None
        assert not list((Path(config.data_dir) / "sessions").iterdir())


def test_http_hanging_native_disconnect_queue_admission_and_shutdown(
    config, executable, monkeypatch
):
    import rkai.execution as execution

    monkeypatch.setattr(execution, "CANCEL_GRACE_SECONDS", 0.1)
    config.allow_cpu_fallback = True
    config.http.queue_capacity = 1
    hanging_model(executable)
    with Library(config) as library:
        install(library, executable)
    with live(config) as (client, service):
        metadata = {
            "model": "vision",
            "capability": "classification",
            "input": {"image": "media:image"},
            "stream": True,
        }
        with client.stream(
            "POST",
            "/api/v1/infer",
            files={
                "metadata": (None, json.dumps(metadata)),
                "image": ("image", executable[1].read_bytes()),
            },
        ) as result:
            assert result.status_code == 200
            lines = result.iter_lines()
            while next(lines) != "event: started":
                pass
            process = service.session.process
            with concurrent.futures.ThreadPoolExecutor() as pool:
                pending = pool.submit(upload, client, executable[1])
                until = time.monotonic() + 5
                while service.admission.requests != 2:
                    assert time.monotonic() < until
                    time.sleep(0.01)
                assert upload(client, executable[1]).status_code == 429
                assert client.get("/health").json()["busy"]
                # Cancel service while active and queued. No result/fallback replay.
                service.stop()
                assert pending.result(timeout=10).status_code >= 400
        until_idle(service)
        assert process.poll() is not None
    with Library(config) as library:
        library.delete("vision")


def test_http_disconnect_reaps_native_before_release(config, executable, monkeypatch):
    import rkai.execution as execution

    monkeypatch.setattr(execution, "CANCEL_GRACE_SECONDS", 0.1)
    config.allow_cpu_fallback = True
    hanging_model(executable)
    with Library(config) as library:
        install(library, executable)
    with live(config) as (client, service):
        with client.stream(
            "POST",
            "/api/v1/infer",
            files={
                "metadata": (
                    None,
                    json.dumps(
                        {
                            "model": "vision",
                            "capability": "classification",
                            "input": {"image": "media:image"},
                            "stream": True,
                        }
                    ),
                ),
                "image": ("image", executable[1].read_bytes()),
            },
        ) as result:
            assert result.status_code == 200
            for line in result.iter_lines():
                if line == "event: started":
                    break
            process = service.session.process
        until_idle(service)
        assert process.poll() is not None
        with Library(config) as library:
            library.delete("vision")


def test_http_queue_and_native_deadline(config, executable, monkeypatch):
    import rkai.execution as execution

    monkeypatch.setattr(execution, "CANCEL_GRACE_SECONDS", 0.1)
    config.allow_cpu_fallback = True
    config.http.request_seconds = 0.7
    hanging_model(executable)
    with Library(config) as library:
        install(library, executable)
    with live(config) as (client, service):
        result = upload(client, executable[1])
        assert result.status_code == 504, result.text
        until_idle(service)
        assert service.session is None
        with Library(config) as library:
            library.delete("vision")


def test_http_output_backpressure_reaps_real_worker(config, executable, monkeypatch):
    import asyncio

    import rkai.service as application
    from rkai.http import OwnedStreamingResponse

    config.allow_cpu_fallback = True
    config.http.drain_seconds = 0.1
    monkeypatch.setattr(application, "QUEUE_EVENTS", 1)
    original = OwnedStreamingResponse.stream_response

    async def slow_send(self, send):
        async def delayed(message):
            if message["type"] == "http.response.body" and message.get("body"):
                await asyncio.sleep(0.3)
            await send(message)

        await original(self, delayed)

    # Deliberately stall the real HTTP writer, retaining the real Session/native worker.
    monkeypatch.setattr(OwnedStreamingResponse, "stream_response", slow_send)
    with Library(config) as library:
        install(library, executable)
    with live(config) as (client, service):
        result = upload(client, executable[1], stream=True)
        assert result.status_code == 200
        events = [
            json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")
        ]
        assert events[-1]["type"] == "failed"
        assert events[-1]["error"]["code"] == "backpressure"
        assert sum(e["type"] in {"completed", "failed", "cancelled"} for e in events) == 1
        until_idle(service)
        assert service.session is None
        with Library(config) as library:
            library.delete("vision")


def test_http_preload_shares_request_context(config, executable):
    config.allow_cpu_fallback = True
    config.http.preload = "vision"
    with Library(config) as library:
        install(library, executable)
    with live(config) as (client, service):
        assert client.get("/health").json()["model"]["state"] == "ready"
        identity = service.session.id
        assert upload(client, executable[1]).status_code == 200
        assert service.session.id == identity


def test_http_interrupted_upload_and_recovery_preserves_live_owner(config, executable):
    from rkai.http_input import recover_uploads
    from rkai.storage import lock

    with Library(config) as library:
        staging = library.root / "http-staging"
        staging.mkdir()
        stale = staging / ("a" * 32)
        stale.mkdir()
        (stale / "image").write_bytes(b"partial media")
        live_upload = staging / ("b" * 32)
        live_upload.mkdir()
        with lock(library.root / "locks" / ("http-upload-" + "b" * 32)):
            recover_uploads(library.root)
            assert live_upload.exists()
            assert not stale.exists()
        recover_uploads(library.root)
        assert not live_upload.exists()
    with live(config) as (client, service):
        # A chunked multipart body exceeds its single file budget mid-part.
        config.http.limits.media_file_bytes = 3
        chunks = [
            b'--CaseBoundary\r\nContent-Disposition: form-data; name="image"; '
            b'filename="../../outside"\r\n\r\n',
            b"abcdefgh",
            b"\r\n--CaseBoundary--\r\n",
        ]
        result = client.post(
            "/api/v1/infer",
            content=iter(chunks),
            headers={"content-type": "multipart/form-data; boundary=CaseBoundary"},
        )
        assert result.status_code == 413
        assert not list((Path(config.data_dir) / "http-staging").iterdir())
        assert service.admission.requests == 0


def test_http_json_default_exceeds_one_mib_and_capability_media_limit(
    config, executable, monkeypatch
):
    import rkai.execution as execution

    config.allow_cpu_fallback = True
    with Library(config) as library:
        install(library, executable)
    with live(config) as (client, _):
        # Large JSON whitespace is valid wire input; no uniform 1 MiB fallback limit.
        raw = b" " * (1024 * 1024 + 1) + b'{"model":"missing","input":"x"}'
        result = client.post(
            "/v1/embeddings", content=raw, headers={"content-type": "application/json"}
        )
        assert result.status_code == 404
        monkeypatch.setattr(execution, "MAX_IMAGE_BYTES", 10)
        result = upload(client, executable[1])
        assert result.status_code == 413
        assert result.json()["error"]["code"] == "input_too_large"


def test_http_concurrent_load_failure_is_shared(config, executable, monkeypatch):
    import rkai.service as application

    with Library(config) as library:
        install(library, executable)
    actual = application.Session
    loading = threading.Event()
    release = threading.Event()
    loads = []

    def delayed_load(*args, **kwargs):
        loads.append(1)
        loading.set()
        assert release.wait(5)
        # Real admission fails: CPU is deliberately disabled, no fake Session.
        return actual(*args, **kwargs)

    monkeypatch.setattr(application, "Session", delayed_load)
    with live(config) as (client, service), concurrent.futures.ThreadPoolExecutor() as pool:
        first = pool.submit(upload, client, executable[1])
        assert loading.wait(5)
        second = pool.submit(upload, client, executable[1])
        until = time.monotonic() + 5
        while service.jobs.qsize() != 1:
            assert time.monotonic() < until
            time.sleep(0.01)
        release.set()
        for result in (first.result(), second.result()):
            assert result.status_code == 422
            assert result.json()["error"]["code"] == "cpu_fallback_disabled"
        assert len(loads) == 1


def test_http_shutdown_during_upload_rejects_and_cleans(config, executable):
    with live(config) as (client, service):

        def closing_body():
            yield b'{"model":"missing",'
            service.stop()
            yield b'"input":"x"}'

        result = client.post(
            "/v1/embeddings", content=closing_body(), headers={"content-type": "application/json"}
        )
        assert result.status_code == 503
        assert result.json()["error"]["code"] == "service_closing"
        until_idle(service)


def test_http_nonstream_disconnect_reaps_real_native(config, executable, monkeypatch):
    import rkai.execution as execution

    monkeypatch.setattr(execution, "CANCEL_GRACE_SECONDS", 0.1)
    config.allow_cpu_fallback = True
    hanging_model(executable)
    with Library(config) as library:
        install(library, executable)
    with live(config) as (client, service):
        metadata = json.dumps(
            {"model": "vision", "capability": "classification", "input": {"image": "media:image"}}
        ).encode()
        body = (
            b'--test-boundary\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'
            + metadata
            + b'\r\n--test-boundary\r\nContent-Disposition: form-data; name="image"; '
            b'filename="image.png"\r\n\r\n'
            + executable[1].read_bytes()
            + b"\r\n--test-boundary--\r\n"
        )
        with socket.create_connection((client.base_url.host, client.base_url.port)) as peer:
            peer.sendall(
                b"POST /api/v1/infer HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: multipart/form-data; boundary=test-boundary\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            until = time.monotonic() + 5
            while not service.session or not service.session.active:
                assert time.monotonic() < until
                time.sleep(0.01)
            process = service.session.process
        until_idle(service)
        assert process.poll() is not None
        with Library(config) as library:
            library.delete("vision")


def test_http_late_cancel_cannot_cancel_reused_session(config, executable, monkeypatch):
    import os
    import signal

    from rkai.execution import Session
    from rkai.service import Job

    config.allow_cpu_fallback = True
    with Library(config) as library:
        install(library, executable)
    release = threading.Event()
    controlled = threading.Event()
    completed = threading.Event()
    targets = []
    actual_cancel, actual_put, actual_send = Session.cancel, Job.put, Session._send

    def delayed_cancel(session, request_id=None):
        if threading.current_thread().name == "rkai-cancel":
            assert release.wait(5)
            targets.append((request_id, session.active))
            actual_cancel(session, request_id)
            controlled.set()
        else:
            actual_cancel(session, request_id)

    def late_cancel(job, event):
        actual_put(job, event)
        if event["type"] == "completed" and not completed.is_set():
            completed.set()
            # Disconnect/timeout at the terminal-yield boundary, before actor finally.
            job.cancel()

    calls = []

    def send(session, message):
        if message["type"] == "infer":
            calls.append(message["request_id"])
            if len(calls) == 2:
                group = session.process.native_group
                os.killpg(group, signal.SIGSTOP)
                try:
                    actual_send(session, message)
                    release.set()
                    assert controlled.wait(5)
                finally:
                    os.killpg(group, signal.SIGCONT)
                return
        actual_send(session, message)

    monkeypatch.setattr(Session, "cancel", delayed_cancel)
    monkeypatch.setattr(Job, "put", late_cancel)
    monkeypatch.setattr(Session, "_send", send)
    with live(config) as (client, service):
        first = upload(client, executable[1])
        identity = service.session.id
        second = upload(client, executable[1])
        assert first.status_code == second.status_code == 200
        assert service.session.id == identity
        assert targets == [(calls[0], calls[1])]


def test_http_deadline_terminal_waits_for_native_stop(config, executable, monkeypatch):
    import rkai.execution as execution

    monkeypatch.setattr(execution, "CANCEL_GRACE_SECONDS", 0.6)
    config.allow_cpu_fallback = True
    config.http.preload = "vision"
    config.http.request_seconds = 0.2
    hanging_model(executable)
    with Library(config) as library:
        install(library, executable)
    with live(config) as (client, service):
        process = service.session.process
        terminals = []
        metadata = {
            "model": "vision",
            "capability": "classification",
            "input": {"image": "media:image"},
            "stream": True,
        }
        with client.stream(
            "POST",
            "/api/v1/infer",
            files={
                "metadata": (None, json.dumps(metadata)),
                "image": ("image", executable[1].read_bytes()),
            },
        ) as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    if event["type"] in {"completed", "failed", "cancelled"}:
                        assert process.poll() is not None
                        terminals.append(event)
        assert len(terminals) == 1
        assert terminals[0]["error"]["code"] == "request_timeout"
        until_idle(service)
        with Library(config) as library:
            library.delete("vision")


def test_http_json_reservation_owned_until_response_sent(config, executable, monkeypatch):
    import asyncio

    from fastapi.responses import JSONResponse

    config.allow_cpu_fallback = True
    config.http.queue_capacity = 0
    with Library(config) as library:
        install(library, executable)
    entered, release = threading.Event(), threading.Event()
    original = JSONResponse.__call__

    async def stalled(self, scope, receive, send):
        if scope["path"] == "/api/v1/infer" and self.status_code == 200:
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        await original(self, scope, receive, send)

    monkeypatch.setattr(JSONResponse, "__call__", stalled)
    with live(config) as (client, service), concurrent.futures.ThreadPoolExecutor() as pool:
        pending = pool.submit(upload, client, executable[1])
        try:
            assert entered.wait(5)
            assert service.admission.requests == 1 and service.admission.memory > 0
            assert upload(client, executable[1]).status_code == 429
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200
        until_idle(service)


@pytest.mark.parametrize("stream", [False, True])
def test_http_slow_response_cannot_succeed_after_deadline(config, executable, monkeypatch, stream):
    import asyncio

    from fastapi.responses import JSONResponse

    from rkai.http import OwnedStreamingResponse

    config.allow_cpu_fallback = True
    config.http.preload = "vision"
    config.http.request_seconds = 0.4
    with Library(config) as library:
        install(library, executable)
    if stream:
        original = OwnedStreamingResponse.stream_response

        async def slow_stream(self, send):
            async def delayed(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    await asyncio.sleep(0.25)
                await send(message)

            await original(self, delayed)

        monkeypatch.setattr(OwnedStreamingResponse, "stream_response", slow_stream)
    else:
        original = JSONResponse.__call__

        async def stalled(self, scope, receive, send):
            if scope["path"] == "/api/v1/infer" and self.status_code == 200:
                await send(
                    {"type": "http.response.start", "status": 200, "headers": self.raw_headers}
                )
                await asyncio.Event().wait()
            await original(self, scope, receive, send)

        monkeypatch.setattr(JSONResponse, "__call__", stalled)
    with live(config) as (client, service):
        start = time.monotonic()
        with pytest.raises(httpx.RemoteProtocolError):
            upload(client, executable[1], stream=stream)
        assert time.monotonic() - start < 1.5
        until_idle(service)


def test_http_expired_queue_reclaims_without_stopping_active_request(
    config, executable, monkeypatch
):
    import rkai.execution as execution

    monkeypatch.setattr(execution, "CANCEL_GRACE_SECONDS", 0.1)
    config.allow_cpu_fallback = True
    config.http.preload = "vision"
    config.http.request_seconds = 5.0
    hanging_model(executable)
    with Library(config) as library:
        install(library, executable)
    with live(config) as (client, service), concurrent.futures.ThreadPoolExecutor() as pool:
        first = pool.submit(upload, client, executable[1])
        until = time.monotonic() + 3
        while not service.session.active:
            assert time.monotonic() < until
            time.sleep(0.01)
        process = service.session.process
        config.http.request_seconds = 0.2
        second = pool.submit(upload, client, executable[1])
        try:
            assert second.result(timeout=1).status_code == 504
            assert not first.done() and process.poll() is None
            assert service.admission.requests == 1 and service.jobs.empty()
            assert len(list((Path(config.data_dir) / "http-staging").iterdir())) == 1
        finally:
            service.stop()
        assert first.result(timeout=5).status_code == 503


def test_http_handshake_deadline_stops_real_guardian(config, executable, monkeypatch, tmp_path):
    import shlex
    import sys

    import rkai.execution as execution

    config.allow_cpu_fallback = True
    config.http.request_seconds = 0.2
    runtime = tmp_path / "slow-runtime"
    (runtime / "bin").mkdir(parents=True)
    python = runtime / "bin/python"
    python.write_text(f'#!/bin/sh\nsleep 2\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    monkeypatch.setattr(execution, "resolve", lambda *_: runtime)
    original = execution.launch_worker
    processes = []

    def launch(*args, **kwargs):
        result = original(*args, **kwargs)
        processes.append(result[0])
        return result

    monkeypatch.setattr(execution, "launch_worker", launch)
    with Library(config) as library:
        install(library, executable)
    with live(config) as (client, service):
        start = time.monotonic()
        result = upload(client, executable[1])
        assert result.status_code == 504
        assert time.monotonic() - start < 1.5
        assert processes and all(p.poll() is not None for p in processes)
        until_idle(service)
        with Library(config) as library:
            library.delete("vision")


def test_http_upload_locks_reclaimed_and_orphans_recovered(config, executable):
    from rkai.storage import lock

    config.allow_cpu_fallback = True
    with Library(config) as library:
        install(library, executable)
        # Earlier M3 upload cleanup left UUID lock files without media directories.
        with lock(library.root / "locks" / ("http-upload-" + "a" * 32)):
            pass
        unrelated = library.root / "locks/http-upload-user-file"
        unrelated.write_text("preserve unrelated data")
    with live(config) as (client, service):
        root = Path(config.data_dir)
        assert not list((root / "locks").glob("http-upload-" + "a" * 32))
        for _ in range(5):
            assert upload(client, executable[1]).status_code == 200
            assert upload(client, executable[1], input={"image": "/etc/passwd"}).status_code == 400
            until_idle(service)
            assert not list((root / "http-staging").iterdir())
            assert list((root / "locks").glob("http-upload-*")) == [unrelated]
        assert unrelated.read_text() == "preserve unrelated data"


def test_http_load_timeout_does_not_poison_queued_same_model(config, executable, monkeypatch):
    import rkai.execution as execution

    config.allow_cpu_fallback = True
    config.http.request_seconds = 1.0
    with Library(config) as library:
        install(library, executable)
    original = execution.handshake
    loading = threading.Event()
    calls = []

    def wait_for_first_timeout(process, connection, timeout, *, cancel=None):
        calls.append(process)
        if len(calls) == 1:
            loading.set()
            assert cancel.wait(5)
        return original(process, connection, timeout, cancel=cancel)

    monkeypatch.setattr(execution, "handshake", wait_for_first_timeout)
    with live(config) as (client, service), concurrent.futures.ThreadPoolExecutor() as pool:
        first = pool.submit(upload, client, executable[1])
        assert loading.wait(5)
        config.http.request_seconds = 10.0
        second = pool.submit(upload, client, executable[1])
        until = time.monotonic() + 3
        while service.jobs.qsize() != 1:
            assert time.monotonic() < until
            time.sleep(0.01)
        assert first.result(timeout=5).status_code == 504
        result = second.result(timeout=10)
        assert result.status_code == 200, result.text
        assert result.json()["capability_result"]["labels"][0]["label"] == "red"
        assert len(calls) == 2
        assert calls[0].poll() is not None
        until_idle(service)
