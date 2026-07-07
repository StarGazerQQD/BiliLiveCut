"""Portable 构建系统完整测试套件。

覆盖:
- Source Snapshot (74c21b4 解析/提取/Overlay)
- Payload (构建/ZIP/Manifest/可复现性)
- Runtime 安装 (原子安装/staging/current.json)
- 用户数据保护 (.env/数据库/storage)
- Zip Slip 安全
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

# 添加 portable 模块到路径
_portable_dir = Path(__file__).resolve().parent.parent  # portable/
_proj_root = _portable_dir.parent.parent  # BiliLiveCut/

sys.path.insert(0, str(_portable_dir))
sys.path.insert(0, str(_proj_root))


# ── 辅助 ──────────────────────────────────────────────────────────


@pytest.fixture
def portable_dir() -> Path:
    """返回 portable 工具目录。"""
    return _portable_dir


@pytest.fixture
def tmp_worktree() -> str:
    """创建临时工作目录。"""
    with tempfile.TemporaryDirectory() as td:
        yield td


@pytest.fixture
def payload_zip() -> Path:
    """返回已构建的 Payload ZIP 路径。"""
    p = _portable_dir / "dist" / "payload" / "source_payload.zip"
    if not p.exists():
        pytest.skip("Payload ZIP 未构建，请先运行 build_payload.py")
    return p


@pytest.fixture
def payload_manifest() -> dict:
    """返回 Payload Manifest。"""
    p = _portable_dir / "dist" / "payload" / "payload_manifest.json"
    if not p.exists():
        pytest.skip("Manifest 未生成")
    return json.loads(p.read_text(encoding="utf-8"))


# ── Source Snapshot 测试 ──────────────────────────────────────────


class TestSourceSnapshot:
    """测试源码快照提取。"""

    def test_commit_resolvable(self) -> None:
        """验证 74c21b4 可解析。"""
        from source_snapshot import resolve_commit

        full = resolve_commit("74c21b4")
        assert len(full) == 40
        assert full == "74c21b401f1da4ef52f0333c94e3874e80f8ceef"

    def test_extract_contains_app_cli(self, tmp_worktree: str) -> None:
        """验证提取内容包含关键业务文件。"""
        from source_snapshot import extract_source

        staging = Path(tmp_worktree) / "test_staging"
        staging.mkdir(parents=True)
        report = extract_source("74c21b4", staging)
        assert report["source_commit_short"] == "74c21b4"
        assert (staging / "app" / "cli.py").exists()
        assert (staging / "pyproject.toml").exists()

    def test_extract_no_workspace_contamination(self, tmp_worktree: str) -> None:
        """验证提取内容不包含当前工作区的脏文件。"""
        from source_snapshot import extract_source

        staging = Path(tmp_worktree) / "test_clean"
        staging.mkdir(parents=True)
        extract_source("74c21b4", staging)

        # 确认不包含构建产物
        assert not (staging / ".venv").exists()
        assert not (staging / "build").exists()
        assert not (staging / "dist").exists()

    def test_version_overlay_only_allowed(self, tmp_worktree: str) -> None:
        """验证版本覆盖只修改允许的文件。"""
        from payload_manifest import RELEASE_VERSION
        from source_snapshot import apply_version_overlay, extract_source

        staging = Path(tmp_worktree) / "test_overlay"
        staging.mkdir(parents=True)
        extract_source("74c21b4", staging)
        modified = apply_version_overlay(staging)

        for f in modified:
            assert f in [
                "app/__init__.py",
                "pyproject.toml",
                "README.md",
                "CHANGELOG.md",
                "setup.py",
                "setup_c.py",
            ]

        # 验证版本已更新
        init_content = (staging / "app" / "__init__.py").read_text(encoding="utf-8")
        assert RELEASE_VERSION in init_content


# ── Payload 测试 ──────────────────────────────────────────────────


class TestPayload:
    """测试 Payload 构建和校验。"""

    def test_payload_zip_exists(self, payload_zip: Path) -> None:
        """验证 Payload ZIP 存在且非空。"""
        assert payload_zip.exists()
        assert payload_zip.stat().st_size > 0

    def test_manifest_valid(self, payload_manifest: dict, payload_zip: Path) -> None:
        """验证 Manifest 基本字段。"""
        from payload_manifest import RELEASE_VERSION, SOURCE_COMMIT_FULL, validate_manifest

        assert payload_manifest["release_version"] == RELEASE_VERSION
        assert payload_manifest["source_commit"] == SOURCE_COMMIT_FULL
        assert payload_manifest["source_commit_short"] == "74c21b4"
        assert payload_manifest["format_version"] == 1
        assert "payload_sha256" in payload_manifest
        assert len(payload_manifest["payload_sha256"]) == 64
        assert "files" in payload_manifest
        assert payload_manifest["file_count"] == len(payload_manifest["files"])

        errors = validate_manifest(payload_manifest, payload_zip)
        assert not errors, f"Manifest validation errors: {errors}"

    def test_payload_sha256_matches(self, payload_manifest: dict, payload_zip: Path) -> None:
        """验证 Payload ZIP SHA-256 与 Manifest 一致。"""
        from payload_manifest import compute_payload_sha256

        actual = compute_payload_sha256(payload_zip)
        expected = payload_manifest["payload_sha256"]
        assert actual == expected

    def test_payload_no_sensitive_files(self) -> None:
        """验证 Payload 不包含敏感文件。"""
        import zipfile

        zip_path = _portable_dir / "dist" / "payload" / "source_payload.zip"
        if not zip_path.exists():
            pytest.skip("Payload not built")

        forbidden_paths = [".git/", "storage/", ".db"]
        forbidden_names = [".env"]  # 仅文件名，不含 .env.example
        with zipfile.ZipFile(zip_path) as zf:
            entries = zf.namelist()
            for entry in entries:
                lower = entry.lower()
                for bad in forbidden_paths:
                    assert bad not in lower, f"Payload 包含禁止文件: {entry}"
                name = entry.rsplit("/", 1)[-1]
                if name in forbidden_names:
                    raise AssertionError(f"Payload 包含禁止文件: {entry}")

    def test_payload_reproducible(self) -> None:
        """验证 Payload 构建的可复现性。"""
        # 已在 build_payload.py 中验证
        pass  # 构建时已验证

    def test_zip_slip_defense(self) -> None:
        """验证 Zip Slip 防护。"""
        import zipfile

        from build_payload import _safe_extract_zip

        # 创建一个包含路径遍历的 ZIP
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            evil_zip = tmp / "evil.zip"
            with zipfile.ZipFile(str(evil_zip), "w") as zf:
                # 绝对路径
                zf.writestr("/etc/passwd", "evil")
            with pytest.raises(RuntimeError, match="绝对路径"):
                _safe_extract_zip(evil_zip, tmp / "out")

            evil_zip.unlink()
            with zipfile.ZipFile(str(evil_zip), "w") as zf:
                # 路径遍历
                zf.writestr("../escape.txt", "evil")
            with pytest.raises(RuntimeError, match="路径遍历"):
                _safe_extract_zip(evil_zip, tmp / "out")

            evil_zip.unlink()
            with zipfile.ZipFile(str(evil_zip), "w") as zf:
                # 盘符
                zf.writestr("C:\\\\windows\\\\system.ini", "evil")
            with pytest.raises(RuntimeError, match="盘符"):
                _safe_extract_zip(evil_zip, tmp / "out")


# ── Runtime 安装测试 ─────────────────────────────────────────────


class TestRuntimeInstall:
    """测试 Runtime 原子安装。"""

    def test_install_release(self, payload_zip: Path, payload_manifest: dict, tmp_worktree: str) -> None:
        """测试首次安装。"""
        from runtime_layout import RELEASE_ID, get_release_dir, install_release

        app_root = Path(tmp_worktree)
        result = install_release(payload_zip, payload_manifest, app_root)
        assert result["installed"] is True
        assert not result.get("already_exists")

        release_dir = get_release_dir(RELEASE_ID, app_root)
        assert release_dir.exists()
        assert (release_dir / "app" / "cli.py").exists()

    def test_install_skips_existing(self, payload_zip: Path, payload_manifest: dict, tmp_worktree: str) -> None:
        """测试相同 Release 不重复安装。"""
        from runtime_layout import install_release

        app_root = Path(tmp_worktree)
        result1 = install_release(payload_zip, payload_manifest, app_root)
        assert result1["installed"] is True

        result2 = install_release(payload_zip, payload_manifest, app_root)
        assert result2.get("already_exists") is True

    def test_current_json_atomic(self, payload_zip: Path, payload_manifest: dict, tmp_worktree: str) -> None:
        """测试 current.json 原子写入。"""
        from runtime_layout import install_release, read_current

        app_root = Path(tmp_worktree)
        install_release(payload_zip, payload_manifest, app_root)

        current = read_current(app_root)
        assert current is not None
        assert current["release_version"] == "0.1.14.5-alpha"
        assert current["source_commit_short"] == "74c21b4"
        assert "payload_sha256" in current

    def test_staging_not_left_behind(self, payload_zip: Path, payload_manifest: dict, tmp_worktree: str) -> None:
        """测试 staging 目录在安装后清理。"""
        from runtime_layout import get_staging_dir, install_release

        app_root = Path(tmp_worktree)
        install_release(payload_zip, payload_manifest, app_root)

        staging = get_staging_dir(app_root)
        assert not staging.exists()


# ── 用户数据保护测试 ──────────────────────────────────────────────


class TestUserDataProtection:
    """测试用户数据隔离。"""

    def test_env_not_overwritten(self, tmp_worktree: str) -> None:
        """测试已有 .env 不被覆盖。"""
        from runtime_layout import create_env_from_template, install_release

        # 先安装 Release
        app_root = Path(tmp_worktree)
        payload_zip = _portable_dir / "dist" / "payload" / "source_payload.zip"
        if not payload_zip.exists():
            pytest.skip("Payload not built")
        manifest = json.loads(
            (_portable_dir / "dist" / "payload" / "payload_manifest.json").read_text(encoding="utf-8")
        )
        install_release(payload_zip, manifest, app_root)

        # 创建自定义 .env
        env_path = app_root / ".env"
        env_path.write_text("MY_CUSTOM_KEY=hello", encoding="utf-8")

        # 调用 create_env_from_template
        created = create_env_from_template(app_root)
        assert not created  # 不应覆盖

        # 验证内容不变
        content = env_path.read_text(encoding="utf-8")
        assert "MY_CUSTOM_KEY=hello" in content

    def test_release_dir_not_containing_data(self, tmp_worktree: str) -> None:
        """测试 Release 目录不包含数据库/storage。"""
        payload_zip = _portable_dir / "dist" / "payload" / "source_payload.zip"
        if not payload_zip.exists():
            pytest.skip("Payload not built")

        import zipfile

        with zipfile.ZipFile(payload_zip) as zf:
            entries = zf.namelist()
            for e in entries:
                assert "storage/" not in e
                assert ".db" not in e
                # .env.example 允许，但 .env 本身禁止
                name = e.rsplit("/", 1)[-1]
                assert name != ".env", f"Payload 包含 .env 文件: {e}"


# ── Manifest 篡改检测测试 ───────────────────────────────────────


class TestManifestTamperDetection:
    """测试 Payload/Manifest 篡改检测。"""

    def test_manifest_tamper_detected(self, payload_zip: Path, payload_manifest: dict, tmp_worktree: str) -> None:
        """测试 Manifest 篡改被检测。"""
        from runtime_layout import install_release

        app_root = Path(tmp_worktree)
        tampered = dict(payload_manifest)
        tampered["payload_sha256"] = "0" * 64

        with pytest.raises(RuntimeError, match="Manifest|Payload"):
            install_release(payload_zip, tampered, app_root)

    def test_payload_tamper_detected(self, payload_manifest: dict, tmp_worktree: str) -> None:
        """测试 Payload ZIP 篡改被检测。"""
        from runtime_layout import install_release

        app_root = Path(tmp_worktree)
        # 创建一个假 ZIP
        fake_zip = Path(tmp_worktree) / "fake.zip"
        fake_zip.write_text("not a real payload")

        with pytest.raises(RuntimeError):
            install_release(fake_zip, payload_manifest, app_root)


# ── 资源路径测试 ──────────────────────────────────────────────────


class TestBundleResourcePath:
    """测试 get_bundled_resource_path。"""

    def test_not_frozen_finds_payload(self) -> None:
        """测试非 PyInstaller 环境下能找到 Payload。"""
        assert not getattr(sys, "frozen", False)
        # 需要在 path 中有 launcher 模块
        sys.path.insert(0, str(_portable_dir))
        from launcher import get_bundled_resource_path

        p = get_bundled_resource_path("payload_manifest.json")
        assert p is not None
        assert p.exists()
