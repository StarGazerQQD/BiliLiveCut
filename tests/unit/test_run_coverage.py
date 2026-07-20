"""覆盖率门禁必须同时反映测试结果与覆盖率结果。"""

from __future__ import annotations

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

    class Completed:
        returncode = 0

    def fake_run(*_args: object, **kwargs: object) -> Completed:
        captured_env.update(kwargs["env"])
        return Completed()

    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    monkeypatch.setattr(run_coverage.subprocess, "run", fake_run)

    assert run_coverage.run_pytest() == 0
    assert captured_env["ASR_NO_MODEL_DOWNLOAD"] == "1"
    assert "WHISPER_MODEL" not in captured_env
