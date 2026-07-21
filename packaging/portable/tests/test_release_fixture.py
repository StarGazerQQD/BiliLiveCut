"""Release fixture isolation tests (V0.1.15)."""

from __future__ import annotations

from pathlib import Path

_PORTABLE_DIR = Path(__file__).resolve().parent.parent
_PROJ_ROOT = _PORTABLE_DIR.parent.parent


def test_release_workflow_has_smoke_tests() -> None:
    """Release workflow 必须包含 smoke-test job。"""
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    assert "smoke-test:" in content, "Release workflow missing smoke-test job"
    assert "--version" in content, "Release workflow missing Lite EXE --version smoke test"
    assert "--doctor" in content, "Release workflow missing Lite EXE --doctor smoke test"
    assert "--diagnose" not in content, "Release workflow calls an unsupported launcher argument"


def test_release_workflow_has_tag_validation() -> None:
    """Release workflow 必须包含 workflow_dispatch tag 校验。"""
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    assert "validate-tag" in content, "Release workflow missing validate-tag job"
    assert "github.event.inputs.tag" in content, (
        "Release workflow must use github.event.inputs.tag for workflow_dispatch"
    )
    assert "needs: validate-tag" in content, "Release test job must be blocked by tag validation"


def test_release_workflow_never_builds_fixture_artifacts() -> None:
    """正式 Release 不得设置 Fixture/CI 绕过变量。"""
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    assert "BLC_FIXTURE_BUILD" not in content
    assert "BLC_CI_BUILD" not in content


def test_release_workflow_explicitly_omits_undistributed_engine_pack() -> None:
    """GitHub Release must not accidentally embed committed fixture metadata."""
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    assert "python build_exe.py --without-engine-pack" in content


def test_release_full_smoke_resolves_bundle_root() -> None:
    """Full ZIP 的版本顶层目录必须先解析再检查组件。"""
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    assert "$bundleRoot" in content
    assert '"$root\\portable-python\\python.exe"' in content
    assert '& "$root\\portable-python\\python.exe" -m venv $venv' in content
    assert "Full bundle offline installation OK" in content


def test_release_full_cli_smoke_uses_offline_venv() -> None:
    """app.cli 必须由已离线安装完整依赖的 Full Bundle venv 导入。"""
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    full_smoke_start = content.index("- name: Full Bundle offline install test")
    cleanup_start = content.index("finally {", full_smoke_start)
    cli_import = '& $venvPython -c "from app.cli import app;'

    assert cli_import in content[full_smoke_start:cleanup_start]
    assert 'python -c "from app.cli import app;' not in content


def test_release_workflow_has_release_audit() -> None:
    """Release workflow 必须运行 release_audit。"""
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    assert "release_audit" in content, "Release workflow missing release_audit step"


def test_release_workflow_has_payload_contract_verify() -> None:
    """Release workflow 必须验证 Payload Manifest/ZIP 契约。"""
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    assert "Payload contract" in content or "format_version" in content, (
        "Release workflow missing Payload contract verification"
    )


def test_release_payload_contract_uses_version_config_source_baseline() -> None:
    """Release workflow 的 Payload 基线校验必须引用版本配置真源。"""
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    assert 'version_config = json.load(open("config/version.json"))' in content
    assert 'manifest["core_source_commit"] == version_config["source_commit_full"]' in content
    assert 'manifest["core_source_commit_short"] == version_config["source_commit_short"]' in content


def test_release_workflow_uploads_crc32() -> None:
    """Release workflow 必须上传 CRC32SUMS。"""
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    assert "CRC32SUMS" in content, "Release workflow missing CRC32SUMS upload"


def test_ci_has_release_audit() -> None:
    """普通 CI 必须包含 release_audit (--quick)。"""
    ci_yml = _PROJ_ROOT / ".github" / "workflows" / "ci.yml"
    content = ci_yml.read_text(encoding="utf-8")
    assert "release_audit" in content, "CI workflow missing release_audit step"


def test_ci_portable_builds_payload() -> None:
    """CI portable-test 必须先构建 Payload。"""
    ci_yml = _PROJ_ROOT / ".github" / "workflows" / "ci.yml"
    content = ci_yml.read_text(encoding="utf-8")
    assert "build_payload.py" in content, "CI portable-test must build Payload (no skip)"
