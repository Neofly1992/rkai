import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from rkai.download import download
from rkai.errors import RkaiError
from rkai.manifest import File

DATA = b"resume-data" * 100000
SPEC = File(path="payload", size=len(DATA), sha256=hashlib.sha256(DATA).hexdigest())


class BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield DATA[: 256 * 1024]
        raise httpx.ReadError("interrupted")


def test_interrupted_transfer_resumes_with_strong_etag(tmp_path):
    calls = []

    def route(request):
        calls.append(dict(request.headers))
        if len(calls) == 1:
            return httpx.Response(200, headers={"ETag": '"fixed"'}, stream=BrokenStream())
        offset = int(request.headers["Range"].split("=")[1].split("-")[0])
        return httpx.Response(
            206,
            headers={
                "ETag": '"fixed"',
                "Content-Range": f"bytes {offset}-{len(DATA) - 1}/{len(DATA)}",
            },
            content=DATA[offset:],
        )

    with httpx.Client(transport=httpx.MockTransport(route)) as http:
        download(http, ["https://example.com/data"], tmp_path / "data", SPEC)
    assert (tmp_path / "data").read_bytes() == DATA
    assert calls[1]["if-range"] == '"fixed"'
    assert len(calls) == 2


@pytest.mark.parametrize("etag", [None, 'W/"weak"'])
def test_weak_or_missing_validator_restarts(tmp_path, etag):
    count = 0

    def route(request):
        nonlocal count
        count += 1
        assert "range" not in request.headers
        headers = {"ETag": etag} if etag else {}
        return httpx.Response(
            200, headers=headers, stream=BrokenStream() if count == 1 else httpx.ByteStream(DATA)
        )

    with httpx.Client(transport=httpx.MockTransport(route)) as http:
        download(http, ["https://example.com/data"], tmp_path / "data", SPEC)
    assert (tmp_path / "data").read_bytes() == DATA


def test_digest_failure_falls_back_without_mixing_bytes(tmp_path):
    hosts = []

    def route(request):
        hosts.append(request.url.host)
        assert "range" not in request.headers
        return httpx.Response(
            200, content=(b"X" * len(DATA) if request.url.host == "bad.example" else DATA)
        )

    with httpx.Client(transport=httpx.MockTransport(route)) as http:
        download(
            http, ["https://bad.example/data", "https://good.example/data"], tmp_path / "data", SPEC
        )
    assert hosts == ["bad.example", "good.example"]
    assert (tmp_path / "data").read_bytes() == DATA


def test_digest_failure_never_installs(tmp_path):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"X" * len(DATA)))
        ) as http,
        pytest.raises(RkaiError) as exc,
    ):
        download(http, ["https://bad.example/data"], tmp_path / "data", SPEC)
    assert exc.value.code == "digest_mismatch"
    assert not list(tmp_path.iterdir())


def test_changed_validator_discards_partial(tmp_path):
    dest = tmp_path / "data"
    (tmp_path / "data.partial").write_bytes(b"wrong bytes")
    (tmp_path / "data.transfer.json").write_text(
        json.dumps({"url": "https://example.com/data", "etag": '"old"'})
    )
    calls = []

    def route(request):
        calls.append(request)
        if "range" in request.headers:
            return httpx.Response(200, headers={"ETag": '"new"'}, content=DATA)
        raise AssertionError("unexpected")

    with httpx.Client(transport=httpx.MockTransport(route)) as http:
        download(http, ["https://example.com/data"], dest, SPEC)
    assert dest.read_bytes() == DATA and len(calls) == 1


def test_real_loopback_transfer(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested_range = self.headers.get("Range")
            requests.append(requested_range)
            offset = int(requested_range.split("=")[1].split("-")[0]) if requested_range else 0
            self.send_response(206 if offset else 200)
            self.send_header("Content-Length", str(len(DATA) - offset))
            self.send_header("ETag", '"local"')
            if offset:
                self.send_header("Content-Range", f"bytes {offset}-{len(DATA) - 1}/{len(DATA)}")
                assert self.headers["If-Range"] == '"local"'
            self.end_headers()
            # First response closes the socket before its advertised length, not a mock error.
            self.wfile.write(DATA[offset:] if offset else DATA[: 256 * 1024])
            self.wfile.flush()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client() as http:
            download(http, [f"http://127.0.0.1:{server.server_port}/data"], tmp_path / "data", SPEC)
        assert (tmp_path / "data").read_bytes() == DATA
        assert requests == [None, "bytes=262144-"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("extra", [0, 1])
def test_complete_invalid_partial_restarts_after_crash(tmp_path, extra):
    (tmp_path / "data.partial").write_bytes(b"X" * (len(DATA) + extra))
    (tmp_path / "data.transfer.json").write_text(
        json.dumps({"url": "https://source.example/data", "etag": '"stable"'})
    )

    def route(request):
        assert "range" not in request.headers
        return httpx.Response(200, content=DATA)

    with httpx.Client(transport=httpx.MockTransport(route)) as http:
        download(http, ["https://source.example/data"], tmp_path / "data", SPEC)
    assert (tmp_path / "data").read_bytes() == DATA


@pytest.mark.parametrize("metadata", [b"[", b"[]", b"null", b'{"etag":42}', b"[" * 2000])
def test_corrupt_resume_metadata_restarts_safely(tmp_path, metadata):
    (tmp_path / "data.partial").write_bytes(b"old")
    (tmp_path / "data.transfer.json").write_bytes(metadata)

    def route(request):
        assert "range" not in request.headers
        return httpx.Response(200, content=DATA)

    with httpx.Client(transport=httpx.MockTransport(route)) as http:
        download(http, ["https://source.example/data"], tmp_path / "data", SPEC)
    assert (tmp_path / "data").read_bytes() == DATA
