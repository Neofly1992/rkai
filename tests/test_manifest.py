import copy
import json

import pytest

from rkai.errors import RkaiError
from rkai.manifest import parse


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(extra=True),
        lambda m: m["files"]["payload"].update(size=True),
        lambda m: m["files"]["payload"].update(path="../outside"),
        lambda m: m["files"]["payload"].update(path="%2e%2e/outside"),
        lambda m: m["files"]["payload"].update(path="a//b"),
        lambda m: m["files"]["payload"].update(path="manifest.json"),
        lambda m: m["files"]["payload"].update(sha256="0" * 63),
        lambda m: m["plans"][0].update(default_capability="missing"),
        lambda m: m["plans"][0]["adapter"].update(config={"nested": ["file:missing"]}),
        lambda m: m["plans"][0]["components"][0]["backend"].update(api=2),
        lambda m: m["plans"].append(copy.deepcopy(m["plans"][0])),
        lambda m: m["sources"].update(s={"kind": "https", "base_url": "https://u:p@x/"}),
        lambda m: m["sources"].update(
            s={
                "kind": "huggingface",
                "origin": "https://x",
                "repository": "org/repo",
                "revision": "main",
            }
        ),
    ],
)
def test_strict_invalid(bundle, mutate):
    _, doc, _ = bundle
    mutate(doc)
    with pytest.raises(RkaiError, match="Invalid manifest"):
        parse(json.dumps(doc).encode())


def test_duplicate_keys_and_nonfinite():
    for raw in (b'{"schema":1,"schema":2}', b'{"value":NaN}'):
        with pytest.raises(RkaiError):
            parse(raw)


def test_closure_includes_nested_backend_config(bundle):
    _, doc, _ = bundle
    doc["files"]["other"] = {**doc["files"]["payload"], "path": "other"}
    doc["plans"][0]["components"][0]["config"] = {"items": [{"source": "file:other"}]}
    m = parse(json.dumps(doc).encode())
    assert m.closure(m.plans[0]) == {"payload", "other"}


@pytest.mark.parametrize("key", ["api", "version"])
def test_versions_reject_boolean(bundle, key):
    _, doc, _ = bundle
    if key == "api":
        doc["plans"][0]["adapter"][key] = True
    else:
        doc["plans"][0]["capabilities"][0][key] = True
    with pytest.raises(RkaiError):
        parse(json.dumps(doc).encode())


def test_cross_host_requires_explicit_selection(bundle, monkeypatch):
    import rkai.manifest as module

    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    m = parse(bundle[0].read_bytes())
    with pytest.raises(RkaiError) as exc:
        m.select(None, None)
    assert exc.value.code == "unsupported_plan"
    assert m.select(None, "rk3588").id == "fixture"


def test_deep_json_is_controlled_error():
    with pytest.raises(RkaiError) as error:
        parse(b"[" * 2000 + b"0" + b"]" * 2000)
    assert error.value.code == "invalid_manifest"
