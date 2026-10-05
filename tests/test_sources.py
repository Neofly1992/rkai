import json

import httpx
import pytest

from rkai.config import Config, Registry
from rkai.errors import RkaiError
from rkai.sources import MAX_SOURCE_ERROR_BYTES, Provider, read_url, resolve_reference


@pytest.mark.parametrize(
    "status,body,upstream_code",
    [
        (403, b'{"Code":10013201001,"Message":"secret-value"}', 10013201001),
        (403, b"<html>secret-value</html>", None),
        (403, b'{"Code":true,"Message":"secret-value"}', None),
        (403, b'{"Code":"secret-value"}', None),
        (404, b'{"Code":404}', 404),
    ],
)
def test_source_http_error_preserves_safe_evidence(status, body, upstream_code):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, content=body))
    ) as http:
        with pytest.raises(RkaiError) as error:
            read_url(http, "https://source.example/data?token=secret-value")
    result = error.value.result()
    details = result["error"]["details"]
    assert details == {
        "http_status": status,
        "source_host": "source.example",
        **({"upstream_code": upstream_code} if upstream_code is not None else {}),
    }
    assert result["error"]["code"] == ("not_found" if status == 404 else "source_http_error")
    assert "secret-value" not in json.dumps(result)


def test_source_error_body_is_bounded_and_closed():
    class ErrorStream(httpx.SyncByteStream):
        consumed = 0
        closed = False

        def __iter__(self):
            while True:
                self.consumed += 4096
                yield b"x" * 4096

        def close(self):
            self.closed = True

    stream = ErrorStream()
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(403, stream=stream))
    ) as http:
        with pytest.raises(RkaiError) as error:
            read_url(http, "https://source.example/data")
    assert error.value.code == "source_http_error"
    assert stream.consumed <= MAX_SOURCE_ERROR_BYTES + 4096
    assert stream.closed


def test_interrupted_source_error_body_keeps_http_evidence():
    class ErrorStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"Code":'
            raise httpx.ReadError("interrupted")

    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(403, stream=ErrorStream()))
    ) as http:
        with pytest.raises(RkaiError) as error:
            read_url(http, "https://source.example/data")
    assert error.value.code == "source_http_error"
    assert error.value.details["http_status"] == 403


@pytest.mark.parametrize(
    "ref,registry,expected",
    [
        ("org/model", None, "hf"),
        ("org/model", "ms", "ms"),
        ("hf://org/model@main", "hf", "hf"),
        ("ms://org/model@master", None, "ms"),
        ("custom:org/model@latest", None, "custom"),
    ],
)
def test_selection(ref, registry, expected):
    c = Config()
    c.registries["custom"] = Registry(kind="https", origin="https://registry.example")
    assert resolve_reference(ref, registry, c).registry == expected


def test_conflict_no_network():
    with pytest.raises(RkaiError) as e:
        resolve_reference("hf://org/model", "ms", Config())
    assert e.value.code == "source_conflict"


def test_latest_is_actual_ref_not_fabricated():
    requests = []

    def route(request):
        requests.append(str(request.url))
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(route)) as http:
        p = Provider(http, Config().registries["hf"])
        with pytest.raises(RkaiError):
            p.fixed_file("org/model", "latest", "manifest.json")
    assert len(requests) == 1 and requests[0].endswith("/revision/latest")


def test_ms_uses_file_commit():
    commit = "a" * 40
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "Success": True,
                    "Data": {"Files": [{"Path": "manifest.json", "Revision": commit}]},
                },
            )
        )
    ) as http:
        revision, urls = Provider(http, Config().registries["ms"]).fixed_file(
            "org/model", "master", "manifest.json"
        )
    assert revision == commit and f"Revision={commit}" in urls[0]


def test_missing_manifest(config):
    def handler(request):
        if "/api/models/" in request.url.path:
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        from rkai.storage import Library

        with Library(config, http=http) as library, pytest.raises(RkaiError) as exc:
            library.pull("hf://org/model")
        assert exc.value.code == "missing_manifest"


def test_mirror_metadata_fallback():
    visits = []

    def route(request):
        visits.append(request.url.host)
        return (
            httpx.Response(503)
            if request.url.host == "mirror.example"
            else httpx.Response(200, content=json.dumps({"sha": "b" * 40}))
        )

    with httpx.Client(transport=httpx.MockTransport(route)) as http:
        p = Provider(
            http,
            Registry(
                kind="huggingface",
                origin="https://origin.example",
                mirrors=["https://mirror.example"],
            ),
        )
        assert p.fixed_file("org/model", "main", "manifest.json")[0] == "b" * 40
    assert visits == ["mirror.example", "origin.example"]


def test_public_uri_allows_mirror_access_settings():
    c = Config()
    c.registries["hf"].mirrors = ["https://mirror.example"]
    assert resolve_reference("hf://org/model", None, c).registry == "hf"


def test_preferred_registry_supplies_mirrors_for_shared_origin():
    from rkai.manifest import HubSource
    from rkai.sources import artifact_urls

    c = Config()
    c.registries["mirror"] = Registry(
        kind="huggingface", origin="https://huggingface.co", mirrors=["https://mirror.example"]
    )
    source = HubSource(
        kind="huggingface",
        origin="https://huggingface.co",
        repository="org/model",
        revision="a" * 40,
    )
    assert artifact_urls(source, "data", c, "mirror")[0].startswith("https://mirror.example/")


def test_static_registry_latest_is_fixed_by_manifest_digest(config, bundle):
    from rkai.storage import Library

    _, doc, content = bundle
    config.registries["static"] = Registry(kind="https", origin="https://registry.example")
    doc["sources"] = {"s": {"kind": "https", "base_url": "https://registry.example/files"}}
    doc["files"]["payload"]["location"] = {"source": "s", "path": "payload.bin"}
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return (
            httpx.Response(200, json=doc)
            if request.url.path.endswith("manifest.json")
            else (httpx.Response(200, content=content))
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        Library(config, http=http) as lib,
    ):
        result = lib.pull("static:org/model@latest", target="rk3588")
        assert result["snapshot"]["selector"] == "latest"
        assert result["snapshot"]["revision"] == result["snapshot"]["manifest_hash"]
        assert requests == ["/org/model/latest/manifest.json", "/files/payload.bin"]


@pytest.mark.parametrize(
    "kind,bad",
    [
        ("huggingface", []),
        ("huggingface", None),
        ("huggingface", "text"),
        ("huggingface", {"sha": []}),
        ("modelscope", []),
        ("modelscope", None),
        ("modelscope", {"Success": True, "Data": None}),
        ("modelscope", {"Success": True, "Data": []}),
        ("modelscope", {"Success": True, "Data": {"Files": None}}),
        ("modelscope", {"Success": True, "Data": {"Files": [None]}}),
        ("modelscope", {"Success": True, "Data": {"Files": [{"Path": 1}]}}),
        (
            "modelscope",
            {"Success": True, "Data": {"Files": [{"Path": "manifest.json", "Revision": None}]}},
        ),
    ],
)
def test_malformed_mirror_metadata_falls_back(kind, bad):
    visits = []
    revision = "c" * 40
    good = (
        {"sha": revision}
        if kind == "huggingface"
        else {"Success": True, "Data": {"Files": [{"Path": "manifest.json", "Revision": revision}]}}
    )

    def route(request):
        visits.append(request.url.host)
        return httpx.Response(
            200, content=json.dumps(bad if request.url.host == "mirror.example" else good)
        )

    settings = Registry(
        kind=kind, origin="https://origin.example", mirrors=["https://mirror.example"]
    )
    with httpx.Client(transport=httpx.MockTransport(route)) as http:
        assert (
            Provider(http, settings).fixed_file("org/model", "main", "manifest.json")[0] == revision
        )
    assert visits == ["mirror.example", "origin.example"]


def test_malformed_origin_metadata_is_uniform_cli_error(config, monkeypatch, capsys):
    from contextlib import contextmanager

    import rkai.storage as storage
    from rkai.cli import main

    @contextmanager
    def malformed_client():
        with httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"null"))
        ) as http:
            yield http

    monkeypatch.setattr(storage, "client", malformed_client)
    assert main(["--data-dir", config.data_dir, "pull", "hf://org/model", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_source_metadata"


@pytest.mark.parametrize("selector", ["%00", "%3f", "%23", "%5c"])
def test_decoded_selector_validation(config, selector):
    with pytest.raises(RkaiError) as error:
        resolve_reference("hf://org/model@" + selector, None, config)
    assert error.value.code == "invalid_reference"


def test_invalid_mirror_manifest_falls_back(config, bundle):
    from rkai.sources import Reference

    settings = config.registries["hf"].model_copy(update={"mirrors": ["https://mirror.example"]})
    calls = []

    def route(request):
        if "/api/" in request.url.path:
            return httpx.Response(200, json={"sha": "a" * 40})
        calls.append(request.url.host)
        return httpx.Response(
            200,
            content=b"<html>mirror unavailable</html>"
            if request.url.host == "mirror.example"
            else bundle[0].read_bytes(),
        )

    with httpx.Client(transport=httpx.MockTransport(route)) as http:
        raw, _ = Provider(http, settings).manifest(Reference("hf", "org/model", "main"))
    assert raw == bundle[0].read_bytes()
    assert calls == ["mirror.example", "huggingface.co"]
