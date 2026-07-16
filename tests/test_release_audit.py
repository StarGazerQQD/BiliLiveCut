"""Release 审计测试 — 验证审计脚本能正确检测故障。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestReleaseAudit:
    """审计脚本功能测试。"""

    def test_audit_script_exists(self) -> None:
        script = REPO_ROOT / "scripts" / "release_audit.py"
        assert script.exists(), "release_audit.py missing"

    def test_audit_importable(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import release_audit  # noqa: E402

        assert hasattr(release_audit, "run_audit")
        assert hasattr(release_audit, "AuditResult")

    def test_audit_quick_passes(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import release_audit  # noqa: E402

        result = release_audit.run_audit(quick=True)
        assert result.exit_code == release_audit.EXIT_OK, f"Quick audit failed: {result.report()}"

    def test_launcher_has_main(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import release_audit  # noqa: E402

        result = release_audit.run_audit(quick=True)
        main_check = any("main()" in p for p in result.passed)
        assert main_check

    def test_no_ci_bypass_in_release(self) -> None:
        release_yml = REPO_ROOT / ".github" / "workflows" / "release.yml"
        if release_yml.exists():
            content = release_yml.read_text(encoding="utf-8")
            assert "BLC_CI_BUILD" not in content, "release.yml still contains BLC_CI_BUILD"

    def test_engine_pack_info_has_crc32(self) -> None:
        info_path = REPO_ROOT / "packaging" / "portable" / "resources" / "engine_pack_info.json"
        if info_path.exists():
            info = json.loads(info_path.read_text(encoding="utf-8"))
            assert info.get("crc32"), "engine_pack_info.json crc32 is empty"
            assert len(str(info.get("crc32", ""))) == 8, "crc32 not 8 hex chars"
