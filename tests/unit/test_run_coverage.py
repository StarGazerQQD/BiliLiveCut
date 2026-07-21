"""覆盖率门禁必须同时反映测试结果与覆盖率结果。"""

from __future__ import annotations

import contextlib
import io

from scripts import run_coverage


def test_main_fails_when_pytest_fails_even_if_coverage_passes(monkeypatch) -> None:
    monkeypatch.setattr(run_coverage, "check_native_extension", lambda: (True, "loaded"))
    monkeypatch.setattr(run_coverage, "run_pytest", lambda: 2)
    monkeypatch.setattr(
        run_coverage,
        "parse_coverage_xml",
        lambda: {"statements": 100, "covered": 60, "missed": 40, "percentage": 60.0},
    )

    assert run_coverage.main() == 2


def test_run_pytest_preserves_whisper_default(monkeypatch) -> None:
    captured_env: dict[str, str] = {}
    captured_cmd: list[str] = []

    class Completed:
        returncode = 0

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        captured_cmd.extend(command)
        captured_env.update(kwargs["env"])
        return Completed()

    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    monkeypatch.setattr(run_coverage.subprocess, "run", fake_run)

    assert run_coverage.run_pytest() == 0
    assert captured_env["ASR_NO_MODEL_DOWNLOAD"] == "1"
    assert "WHISPER_MODEL" not in captured_env
    assert "--fail-on-skip" in captured_cmd


def test_main_output_is_safe_for_windows_legacy_console(monkeypatch) -> None:
    """CI diagnostics must be printable by the Windows cp1252 console."""
    monkeypatch.setattr(
        run_coverage,
        "check_native_extension",
        lambda: (False, "NOT FOUND - C extension not compiled"),
    )
    monkeypatch.setattr(run_coverage, "run_pytest", lambda: 0)
    monkeypatch.setattr(
        run_coverage,
        "parse_coverage_xml",
        lambda: {"statements": 100, "covered": 60, "missed": 40, "percentage": 60.0},
    )
    output = io.BytesIO()
    console = io.TextIOWrapper(output, encoding="cp1252", errors="strict")

    with contextlib.redirect_stdout(console):
        assert run_coverage.main() == 0

    console.flush()
    assert b"additional lines needed" in output.getvalue()
