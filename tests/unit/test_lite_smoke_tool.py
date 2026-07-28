"""The reusable Lite smoke tool must resolve artifacts fail-closed."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import smoke_portable_lite

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_required_file_rejects_missing_current_artifact(tmp_path: Path) -> None:
    (tmp_path / "old.exe").write_bytes(b"old")

    with pytest.raises(RuntimeError, match="Missing Lite executable"):
        smoke_portable_lite._required_file(tmp_path, "current.exe", "Lite executable")


def test_main_passes_resolved_artifacts_to_smoke(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    lite_dir = tmp_path / "lite"
    engine_dir = tmp_path / "engine"
    lite_dir.mkdir()
    engine_dir.mkdir()
    version_config = json.loads(smoke_portable_lite.VERSION_CONFIG.read_text(encoding="utf-8"))
    version = version_config["release_version"]
    executable = lite_dir / version_config["naming"]["lite_exe"].format(version=version)
    engine_pack = engine_dir / version_config["naming"]["engine_pack_zip"].format(version=version)
    executable.write_bytes(b"exe")
    engine_pack.write_bytes(b"zip")
    observed: list[tuple[Path, Path]] = []

    def fake_run_smoke(actual_executable: Path, actual_engine_pack: Path, **_kwargs: object) -> None:
        observed.append((actual_executable, actual_engine_pack))

    monkeypatch.setattr(smoke_portable_lite, "run_smoke", fake_run_smoke)

    assert (
        smoke_portable_lite.main(
            [
                "--lite-dir",
                str(lite_dir),
                "--engine-pack-dir",
                str(engine_dir),
            ]
        )
        == 0
    )
    assert observed == [(executable.resolve(), engine_pack.resolve())]


def test_main_prints_utf8_launcher_log_on_legacy_console(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """The Windows smoke driver must survive Chinese launcher diagnostics on cp1252 hosts."""
    lite_dir = tmp_path / "lite"
    engine_dir = tmp_path / "engine"
    lite_dir.mkdir()
    engine_dir.mkdir()
    version_config = json.loads(smoke_portable_lite.VERSION_CONFIG.read_text(encoding="utf-8"))
    version = version_config["release_version"]
    (lite_dir / version_config["naming"]["lite_exe"].format(version=version)).write_bytes(b"exe")
    (engine_dir / version_config["naming"]["engine_pack_zip"].format(version=version)).write_bytes(b"zip")
    log_path = tmp_path / "launcher.log"
    log_path.write_text("模型准备完成：中文日志", encoding="utf-8")

    stdout_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="cp1252", errors="strict")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(
        smoke_portable_lite,
        "run_smoke",
        lambda *_args, **_kwargs: smoke_portable_lite._print_log(log_path),
    )

    result = smoke_portable_lite.main(["--lite-dir", str(lite_dir), "--engine-pack-dir", str(engine_dir)])
    stdout.flush()
    stderr.flush()

    assert result == 0
    assert stdout.encoding.lower().replace("_", "-") == "utf-8"
    assert "模型准备完成：中文日志" in stdout_bytes.getvalue().decode("utf-8")
