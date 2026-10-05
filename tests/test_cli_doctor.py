import json

import pytest

from rkai.cli import main
from rkai.config import Config, load
from rkai.doctor import doctor
from rkai.errors import RkaiError


@pytest.mark.parametrize("profile", ["release", "debug"])
def test_profiles_identical_rules_and_stdout(config, bundle, capsys, profile):
    code = main(
        [
            "--data-dir",
            config.data_dir,
            "--profile",
            profile,
            "pull",
            str(bundle[0]),
            "--target",
            "rk3588",
            "--alias",
            profile,
            "--json",
        ]
    )
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert code == 0
    assert result["file_state"] == "ready" and result["execution_validation"] == "unverified"
    assert "operation_id=" in out.err
    assert ("profile=" in out.err) is (profile == "debug")
    assert config.data_dir not in out.err


def test_log_override(config, bundle, capsys):
    assert (
        main(
            [
                "--data-dir",
                config.data_dir,
                "--profile",
                "debug",
                "--log-level",
                "ERROR",
                "pull",
                str(bundle[0]),
                "--target",
                "rk3588",
                "--json",
            ]
        )
        == 0
    )
    assert capsys.readouterr().err == ""


def test_config_precedence_and_validation(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path}/configured"\nprofile="debug"\nlog_level="WARNING"')
    c = load(str(path), str(tmp_path / "override"))
    assert c.data_dir == str(tmp_path / "override") and c.log_level == "WARNING"
    path.write_text('data_dir="relative"')
    with pytest.raises(RkaiError):
        load(str(path))


def test_missing_required_arguments_and_m3_rejected(capsys):
    for command in ("run", "convert", "serve"):
        assert main([command, "--json"]) == 2
        result = json.loads(capsys.readouterr().out)
        assert result["error"]["code"] == "invalid_arguments"


def test_static_doctor_unknown_never_ready(config, tmp_path):
    report = doctor(config, probe=True, system_root=tmp_path / "fake-system")
    checks = {c["name"]: c for c in report["checks"]}
    assert report["status"] != "ready"
    assert checks["compatibility"]["status"] == "unknown"
    assert checks["soc"]["status"] == "unknown"
    assert checks["probe"]["status"] == "skipped"
    assert report["active_probe"] == "not_run"


def test_doctor_file_state_is_not_execution_validation(config, bundle):
    from rkai.storage import Library

    with Library(config) as lib:
        lib.pull(str(bundle[0]), alias="m", target="rk3588")
    report = doctor(config, model="m")
    checks = {c["name"]: c["status"] for c in report["checks"]}
    assert checks["model_files"] == "pass"
    assert checks["model_execution"] == "unknown"


def test_doctor_invalid_runtime(tmp_path):
    library = tmp_path / "runtime.so"
    library.write_bytes(b"not an ELF")
    c = Config(data_dir=str(tmp_path / "data"), runtime_paths={"rknn": str(library)})
    checks = {v["name"]: v for v in doctor(c)["checks"]}
    assert checks["rknn_elf"]["status"] == "fail"


def test_malformed_config_returns_uniform_error(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text('registries="not a table"')
    assert main(["--config", str(path), "doctor", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "invalid_config"
    assert result["checks"][0]["status"] == "fail"
    assert result["status"] == "unavailable"


def test_regular_file_cannot_pass_as_npu_device(config, tmp_path):
    root = tmp_path / "fake-system"
    (root / "dev").mkdir(parents=True)
    (root / "dev/rknpu").write_bytes(b"not a device node")
    checks = {c["name"]: c for c in doctor(config, system_root=root)["checks"]}
    assert checks["npu_device"]["status"] == "fail"


@pytest.mark.parametrize(
    "entry,expected",
    [
        (b"1: 0 8 FUNC GLOBAL DEFAULT 12 rknn_query", "pass"),
        (b"1: 0 8 FUNC GLOBAL DEFAULT 12 rknn_query_fake", "unknown"),
        (b"1: 0 8 FUNC GLOBAL DEFAULT UND rknn_query", "unknown"),
        (b"1: 0 8 FUNC GLOBAL DEFAULT 12 rknn_query@@V1", "pass"),
    ],
)
def test_doctor_requires_exact_defined_symbol(config, tmp_path, monkeypatch, entry, expected):
    import subprocess

    library = tmp_path / "runtime.so"
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = (183).to_bytes(2, "little")
    library.write_bytes(header)
    config.runtime_paths["rknn"] = str(library)
    monkeypatch.setattr("rkai.doctor.shutil.which", lambda _: "/usr/bin/readelf")
    monkeypatch.setattr(
        "rkai.doctor.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=entry),
    )
    checks = {c["name"]: c for c in doctor(config)["checks"]}
    assert checks["rknn_symbols"]["status"] == expected


def test_doctor_fifo_runtime_does_not_block(config, tmp_path):
    import os

    runtime = tmp_path / "runtime.so"
    os.mkfifo(runtime)
    config.runtime_paths["rknn"] = str(runtime)
    checks = {c["name"]: c for c in doctor(config)["checks"]}
    assert checks["rknn_runtime"]["status"] == "fail"
