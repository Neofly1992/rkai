"""Real public small-file evidence. Not a runtime/model support demonstration."""

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from rkai.config import Config
from rkai.errors import RkaiError
from rkai.manifest import parse
from rkai.sources import Provider, Reference, client, read_url
from rkai.storage import Library


def run(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    if list(root.iterdir()):
        raise ValueError("Evidence directory must be empty")
    records = []
    sources = {}
    files = {}
    with client() as http:
        config = Config(data_dir=str(root / "library"))
        for name, repo, selector, filename in (
            ("hf", "google-bert/bert-base-uncased", "main", "config.json"),
            ("ms", "pengzhendong/wetext", "master", "configuration.json"),
        ):
            provider = Provider(http, config.registries[name])
            revision, urls = provider.fixed_file(repo, selector, filename)
            raw = read_url(http, urls[-1])
            sha256 = hashlib.sha256(raw).hexdigest()
            record = {
                "registry": name,
                "repository": repo,
                "selector": selector,
                "revision": revision,
                "path": filename,
                "size": len(raw),
                "sha256": sha256,
                "digest_evidence": "locally measured from public HTTPS",
                "url": urls[-1],
            }
            records.append(record)
            sources[name] = {
                "kind": config.registries[name].kind,
                "origin": config.registries[name].origin,
                "repository": repo,
                "revision": revision,
            }
            files[name] = {
                "path": name + "/" + filename,
                "size": len(raw),
                "sha256": sha256,
                "location": {"source": name, "path": filename},
            }
            try:
                provider.manifest(Reference(name, repo, selector))
            except RkaiError as e:
                record["missing_manifest_result"] = e.code
                assert e.code == "missing_manifest"
            else:
                raise AssertionError("Fixture repository unexpectedly has a runtime manifest")
        hf = records[0]
        sources["custom"] = {
            "kind": "https",
            "base_url": (f"https://huggingface.co/{hf['repository']}/resolve/{hf['revision']}"),
        }
        files["custom"] = {
            "path": "custom/config.json",
            "size": hf["size"],
            "sha256": hf["sha256"],
            "location": {"source": "custom", "path": "config.json"},
        }
        # Independent data-only acquisition manifest. No example weights/profiles involved.
        manifest = {
            "schema": "rkai.model/v0",
            "model": {
                "id": "verification/public-metadata",
                "revision": hashlib.sha256(
                    json.dumps(records, sort_keys=True).encode()
                ).hexdigest(),
            },
            "sources": sources,
            "files": files,
            "plans": [
                {
                    "id": "acquisition-only",
                    "target": {
                        "os": "linux",
                        "arch": "aarch64",
                        "accelerator": "rockchip-npu",
                        "soc": "rk3588",
                    },
                    "capabilities": [{"id": "acquisition-evidence", "version": 1}],
                    "default_capability": "acquisition-evidence",
                    "adapter": {"id": "verification-only", "api": 1, "config": {}},
                    "components": [
                        {
                            "id": "metadata",
                            "backend": {"id": "not-executable", "api": 1},
                            "compatibility": "verification-only/not-executable",
                            "files": {"hf": "hf", "ms": "ms", "custom": "custom"},
                        }
                    ],
                }
            ],
        }
        path = root / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        parse(path.read_bytes())
        with Library(config, http=http) as library:
            installed = library.pull(str(path), alias="public-evidence", target="rk3588")
            second = library.pull(str(path), alias="shared-evidence", target="rk3588")
            assert installed["installation"] == second["installation"]
            first_delete = library.delete("public-evidence")
            assert not first_delete["removed_installation"]
        # Real SQLite close/reopen and isolated deletion.
        with Library(config) as library:
            with library.lease("shared-evidence"):
                pass
            last_delete = library.delete("shared-evidence")
            assert last_delete["removed_installation"]
        return {
            "kind": "real_public_https_and_sqlite",
            "fixtures": records,
            "installed": installed,
            "shared_alias_and_reopen": "passed",
            "custom_https_file_source": "passed",
            "external_manifest_preserved": path.exists(),
            "npu_execution": "not_run",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    if args.work_dir:
        result = run(args.work_dir.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="rkai-public-") as temporary:
            result = run(Path(temporary).resolve())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
