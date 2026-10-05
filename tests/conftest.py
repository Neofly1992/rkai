import hashlib
import json

import pytest

from rkai.config import Config


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=str(tmp_path / "library"))


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "external"
    root.mkdir()
    content = b"independent acquisition fixture; not executable\n"
    (root / "payload.bin").write_bytes(content)
    doc = {
        "schema": "rkai.model/v0",
        "model": {"id": "test/artifact", "revision": "r1"},
        "sources": {},
        "files": {
            "payload": {
                "path": "payload.bin",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        },
        "plans": [
            {
                "id": "fixture",
                "target": {
                    "os": "linux",
                    "arch": "aarch64",
                    "accelerator": "rockchip-npu",
                    "soc": "rk3588",
                },
                "capabilities": [{"id": "fixture", "version": 1}],
                "default_capability": "fixture",
                "adapter": {"id": "acquisition-fixture", "api": 1, "config": {}},
                "components": [
                    {
                        "id": "data",
                        "backend": {"id": "fixture", "api": 1},
                        "compatibility": "not-executable/fixture",
                        "files": {"data": "payload"},
                    }
                ],
            }
        ],
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(doc))
    return path, doc, content
