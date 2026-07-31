"""Release tooling must cover the complete tracked source set and fail closed."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import yaml

import conftest as release_pytest
from scripts import run_ruff

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture
    from _pytest.monkeypatch import MonkeyPatch


def test_tracked_ruff_scope_includes_previously_missed_files() -> None:
    """Ruff's release scope includes tracked sources and excludes pending deletions."""
    files = set(run_ruff.tracked_python_files())
    assert "packaging/portable/config/model_catalog.py" in files
    assert "scripts/run_coverage.py" in files
    assert all((run_ruff.REPO_ROOT / path).is_file() for path in files)


def test_fail_on_skip_option_sets_failing_exit_status() -> None:
    """A skipped test changes an otherwise successful release session to failure."""

    class Config:
        pluginmanager = SimpleNamespace(
            get_plugin=lambda _name: SimpleNamespace(
                stats={"skipped": [object()]},
                write_sep=lambda *_args, **_kwargs: None,
            )
        )

        @staticmethod
        def getoption(name: str) -> bool:
            return name == "--fail-on-skip"

    session = SimpleNamespace(config=Config(), exitstatus=pytest.ExitCode.OK)
    release_pytest.pytest_sessionfinish(session, pytest.ExitCode.OK)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_release_gate_cannot_disable_payload_or_portable_checks() -> None:
    """The release gate exposes no skip flags or reproducibility bypass."""
    source = (run_ruff.REPO_ROOT / "scripts" / "release_gate.py").read_text(encoding="utf-8")
    assert "--skip-payload" not in source
    assert "--skip-portable" not in source
    assert "--skip-reproducible" not in source


def test_ci_gate_runs_ruff_with_current_python(monkeypatch: MonkeyPatch) -> None:
    """本地 CI 门禁不得依赖 PATH 中的裸 ``ruff`` 可执行文件。"""
    from scripts import ci_gate

    commands: list[list[str]] = []

    def capture_run(command: list[str], **_kwargs: object) -> bool:
        commands.append(command)
        return True

    monkeypatch.setattr(ci_gate, "_run", capture_run)
    monkeypatch.setattr(ci_gate, "_pytest", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ci_gate.sys, "argv", ["ci_gate.py", "--skip-audit"])

    assert ci_gate.main() == 0
    assert commands[:2] == [
        [sys.executable, "scripts/run_ruff.py", "check"],
        [sys.executable, "scripts/run_ruff.py", "format"],
    ]


def test_ci_gate_uses_workspace_local_pytest_directories(monkeypatch: MonkeyPatch) -> None:
    """受限 Windows 环境不得要求访问用户级 pytest 临时目录。"""
    from scripts import ci_gate

    captured: list[str] = []

    def capture_run(command: list[str], **_kwargs: object) -> bool:
        captured.extend(command)
        return True

    monkeypatch.setattr(ci_gate, "_run", capture_run)

    assert ci_gate._pytest("tests/") is True
    expected_root = ci_gate.REPO_ROOT / "packaging" / "portable" / "build" / "ci-gate"
    assert expected_root.is_dir()
    basetemp = Path(next(arg.removeprefix("--basetemp=") for arg in captured if arg.startswith("--basetemp=")))
    cache_dir = Path(next(arg.removeprefix("cache_dir=") for arg in captured if arg.startswith("cache_dir=")))
    assert basetemp.name == "pytest"
    assert cache_dir.name == "cache"
    assert basetemp.parent.parent == expected_root
    assert cache_dir.parent == basetemp.parent
    assert not basetemp.parent.exists()


def test_release_gate_uses_workspace_local_pytest_directories(monkeypatch: MonkeyPatch) -> None:
    """本地发布门禁与 CI 门禁必须使用相同的受控临时目录策略。"""
    from scripts import release_gate

    captured: list[str] = []

    def capture_run(command: list[str], **_kwargs: object) -> bool:
        captured.extend(command)
        return True

    monkeypatch.setattr(release_gate, "_run", capture_run)

    assert release_gate._pytest("packaging/portable/tests/") is True
    expected_root = release_gate.REPO_ROOT / "packaging" / "portable" / "build" / "release-gate-local"
    assert expected_root.is_dir()
    basetemp = Path(next(arg.removeprefix("--basetemp=") for arg in captured if arg.startswith("--basetemp=")))
    cache_dir = Path(next(arg.removeprefix("cache_dir=") for arg in captured if arg.startswith("cache_dir=")))
    assert basetemp.name == "pytest"
    assert cache_dir.name == "cache"
    assert basetemp.parent.parent == expected_root
    assert cache_dir.parent == basetemp.parent
    assert not basetemp.parent.exists()


def test_rust_build_uses_current_python_interpreter() -> None:
    """PyO3 构建必须显式使用当前虚拟环境的 Python。"""
    source = (run_ruff.REPO_ROOT / "tools" / "native" / "build_rust.py").read_text(encoding="utf-8")
    assert 'env.setdefault("PYO3_PYTHON", sys.executable)' in source


def test_rust_build_uses_exact_windows_platform_match() -> None:
    """darwin 不得因名称包含 win 而被误判为 Windows。"""
    from tools.native import build_rust

    assert build_rust._extension_suffix("win32") == ".pyd"
    assert build_rust._extension_suffix("darwin") == ".so"
    assert build_rust._extension_suffix("linux") == ".so"


def test_rust_build_reconfigures_console_to_utf8() -> None:
    """Windows runner 的 cp1252 文本流必须能安全输出中文日志。"""
    from tools.native import build_rust

    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="cp1252", errors="strict")
    build_rust._configure_stream_encoding(stream)
    stream.write("Rust 加速模块编译")
    stream.flush()

    assert stream.encoding.lower() == "utf-8"
    assert output.getvalue().decode("utf-8") == "Rust 加速模块编译"


def test_rust_build_streams_cargo_output(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Cargo 构建继承控制台输出，不再缓存到进程结束。"""
    from tools.native import build_rust

    rust_source = tmp_path / "rust"
    release_dir = rust_source / "target" / "release"
    release_dir.mkdir(parents=True)
    (rust_source / "Cargo.toml").write_text("[package]\nname='fixture'\n", encoding="utf-8")
    source_suffix = ".dll" if sys.platform == "win32" else ".so"
    destination_suffix = ".pyd" if sys.platform == "win32" else ".so"
    (release_dir / f"_rust_cluster{source_suffix}").write_bytes(b"native")
    target_dir = tmp_path / "analysis"

    observed_kwargs: dict[str, object] = {}

    def fake_run(*_args: object, **kwargs: object) -> SimpleNamespace:
        observed_kwargs.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_rust, "RUST_SRC", rust_source)
    monkeypatch.setattr(build_rust, "TARGET_DIR", target_dir)
    monkeypatch.setattr(build_rust.subprocess, "run", fake_run)

    assert build_rust.build() is True
    assert "capture_output" not in observed_kwargs
    assert observed_kwargs["env"]["PYO3_PYTHON"] == sys.executable  # type: ignore[index]
    assert (target_dir / f"_rust_cluster{destination_suffix}").read_bytes() == b"native"


def test_windows_payload_jobs_run_on_windows_and_verify_native_modules() -> None:
    """CI 和 Release 必须在 Windows 构建目标平台原生模块。"""
    release_workflow = yaml.safe_load(
        (run_ruff.REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    ci_workflow = yaml.safe_load((run_ruff.REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))

    assert release_workflow["jobs"]["build-payload"]["runs-on"] == "windows-latest"
    assert ci_workflow["jobs"]["portable-test"]["runs-on"] == "windows-latest"

    release_source = (run_ruff.REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "Missing Windows native modules" in release_source
    assert "Foreign native modules in Windows Payload" in release_source
    assert "Full Bundle native acceleration OK" in release_source


def test_release_gate_audits_portable_runtime_locks() -> None:
    source = (run_ruff.REPO_ROOT / "scripts" / "release_gate.py").read_text(encoding="utf-8")

    assert '"scripts/audit_portable_runtime_locks.py"' in source


def test_source_manifest_job_installs_pinned_cython() -> None:
    """禁用构建隔离的 source manifest 校验必须预装固定版本 Cython。"""
    release_workflow = yaml.safe_load(
        (run_ruff.REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    steps = release_workflow["jobs"]["build-sdist"]["steps"]
    install_step = next(step for step in steps if step.get("name") == "Install build tools")

    assert "Cython==3.2.9" in install_step["run"]
    assert "--no-build-isolation" in next(
        step["run"] for step in steps if step.get("name") == "Validate source manifest"
    )


def test_direct_setup_builds_install_declared_backend_requirements() -> None:
    """Direct setup.py calls must not depend on a runner's ambient setuptools."""
    matched_commands: list[str] = []
    for workflow_name in ("ci.yml", "release.yml"):
        workflow = yaml.safe_load(
            (run_ruff.REPO_ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
        )
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                command = step.get("run", "")
                if "python setup.py build_ext --inplace" in command:
                    matched_commands.append(command)
                    assert '"setuptools>=77"' in command
                    assert '"Cython==3.2.9"' in command

    assert len(matched_commands) == 2


def test_windows_c_extension_compiles_utf8_source() -> None:
    """Windows 扩展必须按 UTF-8 编译并启用确定性链接。"""
    for build_script in ("setup.py", "setup_c.py"):
        source = (run_ruff.REPO_ROOT / build_script).read_text(encoding="utf-8")
        assert '"/utf-8"' in source
        assert 'extra_link_args=(["/Brepro"] if sys.platform == "win32" else [])' in source


def test_release_gate_rejects_stale_payload_after_build_failure(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    """Payload 构建失败后不得继续把已有旧产物报告为有效。"""
    from scripts import release_gate

    monkeypatch.setattr(release_gate, "_run", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(release_gate, "_pytest", lambda *_args, **_kwargs: False)

    assert release_gate.main() == 1
    assert "Payload 构建失败；拒绝校验已有产物" in capsys.readouterr().out
