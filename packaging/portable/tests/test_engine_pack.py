"""Engine Pack 与模型安装测试套件。

覆盖:
* Engine Pack Manifest 构建与校验
* CRC32 流式计算与十六进制格式
* 正确本地包路径 — CRC 成功 → 网络 0
* CRC32 错误路径 — CRC 失败 → 全量下载四模型
* 包缺失路径 — 本地不存在 → 全量下载四模型
* Zip Slip 防护
* 安装清单读写
* 原子安装与回滚
* 已安装模型跳过安装
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
import zlib
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    pass

# 添加 portable 模块到路径 (与 test_portable.py 一致)
_portable_dir = Path(__file__).resolve().parent.parent  # portable/
_proj_root = _portable_dir.parent.parent  # BiliLiveCut/
_src_dir = _portable_dir / "src"  # portable/src/
sys.path.insert(0, str(_portable_dir))
sys.path.insert(0, str(_proj_root))
sys.path.insert(0, str(_src_dir))

from blc_portable.engine_pack.manifest import MANIFEST_FORMAT_VERSION  # noqa: E402
from blc_portable.payload.manifest import RELEASE_VERSION as _EP_RELEASE_VERSION  # noqa: E402

# ── 测试辅助 ────────────────────────────────────────────────


@pytest.fixture
def tmp_app_root() -> Generator[Path, None, None]:
    """创建临时应用根目录。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "models").mkdir(exist_ok=True)
        (root / "runtime").mkdir(exist_ok=True)
        yield root


@pytest.fixture
def fixture_engine_pack() -> Generator[Path, None, None]:
    """生成测试用 Fixture Engine Pack (小型占位文件)."""
    with tempfile.TemporaryDirectory() as pack_tmp:
        pack_dir = Path(pack_tmp)
        staging = pack_dir / "ep-staging"
        staging.mkdir(exist_ok=True)

        engines = ["whisper", "paraformer", "sensevoice", "funasr_nano"]
        for eng in engines:
            eng_dir = staging / "models" / eng
            eng_dir.mkdir(parents=True)
            (eng_dir / "model.bin").write_bytes(b"fixture-model-data-" + eng.encode() * 10)
            (eng_dir / "config.json").write_text('{"_fixture": true}', encoding="utf-8")

        manifest: dict[str, Any] = {
            "format_version": MANIFEST_FORMAT_VERSION,
            "engine_pack_version": _EP_RELEASE_VERSION,
            "portable_release_version": _EP_RELEASE_VERSION,
            "source_commit": "7c2764bae599f3e173f8bf63463baf961013650a",
            "source_commit_short": "7c2764b",
            "archive_filename": "test.engine.pack.zip",
            "archive_crc32": "",
            "archive_sha256": "",
            "total_files": 8,
            "engines": [
                {
                    "engine_id": e,
                    "engine_name": e,
                    "model_id": e,
                    "hub": "modelscope",
                    "revision": "v2.0.4",
                    "target_path": f"models/{e}",
                    "model_repo": None,
                    "sub_models": [],
                }
                for e in engines
            ],
            "files": {},
        }
        (staging / "engine-pack-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 打包 ZIP (单次)
        zip_path = pack_dir / "test.engine.pack.zip"

        # 先计算 SHA-256 (打包后)
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(staging.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(staging).as_posix())

        # 计算 archive 级别的 SHA-256 和 CRC32
        import hashlib

        sha256_hasher = hashlib.sha256()
        crc_val = 0
        with open(str(zip_path), "rb") as f:
            while True:
                chunk = f.read(8 * 1024 * 1024)
                if not chunk:
                    break
                sha256_hasher.update(chunk)
                crc_val = zlib.crc32(chunk, crc_val)

        archive_sha256 = sha256_hasher.hexdigest()
        archive_crc32 = f"{crc_val & 0xFFFFFFFF:08X}"

        # 更新 manifest 并重新打包
        manifest["archive_crc32"] = archive_crc32
        manifest["archive_sha256"] = archive_sha256
        (staging / "engine-pack-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        zip_path.unlink()
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(staging.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(staging).as_posix())

        shutil.rmtree(str(staging))
        yield zip_path


# ── Manifest 测试 ────────────────────────────────────────────


class TestEnginePackManifest:
    """Engine Pack Manifest 数据结构与校验测试。"""

    def test_engines_definition(self) -> None:
        """四引擎定义列表-中四个引擎都存在。"""
        from blc_portable.engine_pack.manifest import ENGINES

        ids = [e["engine_id"] for e in ENGINES]
        assert "whisper" in ids
        assert "paraformer" in ids
        assert "sensevoice" in ids
        assert "funasr_nano" in ids

    def test_paraformer_has_sub_models(self) -> None:
        """Paraformer 包含三个子模型 (fsmn-vad / ct-punc / cam++)."""
        from blc_portable.engine_pack.manifest import ENGINES

        pfm = next(e for e in ENGINES if e["engine_id"] == "paraformer")
        subs = pfm.get("sub_models", [])
        sub_ids = [s["model_id"] for s in subs]
        # model_catalog 使用完整仓库 ID
        assert any("fsmn_vad" in sid.lower() for sid in sub_ids), f"缺少 fsmn-vad: {sub_ids}"
        assert any("ct-punc" in sid.lower() or "punc_ct" in sid.lower() for sid in sub_ids), f"缺少 ct-punc: {sub_ids}"
        assert any("campplus" in sid.lower() or "cam_" in sid.lower() for sid in sub_ids), f"缺少 cam++: {sub_ids}"

    def test_create_manifest(self) -> None:
        """create_manifest 生成有效的 Manifest。"""
        from blc_portable.engine_pack.manifest import create_manifest

        m = create_manifest(
            source_commit="7c2764bae599f3e173f8bf63463baf961013650a",
            archive_crc32="1234ABCD",
            archive_sha256="a" * 64,
            file_list={},
        )

        assert m.format_version == MANIFEST_FORMAT_VERSION
        assert m.engine_pack_version == _EP_RELEASE_VERSION
        assert m.archive_crc32 == "1234ABCD"
        assert len(m.engines) == 4
        assert m.get_engine_ids() == ["whisper", "paraformer", "sensevoice", "funasr_nano"]

    def test_validate_manifest_valid(self) -> None:
        """完整 Manifest 校验通过。"""
        from blc_portable.engine_pack.manifest import create_manifest, validate_manifest

        m = create_manifest(
            source_commit="7c2764bae599f3e173f8bf63463baf961013650a",
            archive_crc32="1234ABCD",
            archive_sha256="a" * 64,
            file_list={},
        )
        errors = validate_manifest(m)
        assert errors == []

    def test_validate_manifest_missing_engine(self) -> None:
        """缺少引擎时校验报错。"""
        from blc_portable.engine_pack.manifest import EnginePackManifest, validate_manifest

        m = EnginePackManifest(
            format_version=MANIFEST_FORMAT_VERSION,
            engine_pack_version=_EP_RELEASE_VERSION,
            portable_release_version=_EP_RELEASE_VERSION,
            source_commit="7c2764bae599f3e173f8bf63463baf961013650a",
            source_commit_short="7c2764b",
            archive_filename="test.zip",
            archive_crc32="1234ABCD",
            archive_sha256="a" * 64,
            engines=[],
        )
        errors = validate_manifest(m)
        assert len(errors) > 0

    def test_validate_crc32_format(self) -> None:
        """CRC32 格式应为 8 位大写十六进制。"""
        from blc_portable.engine_pack.manifest import EnginePackManifest, validate_manifest

        m = EnginePackManifest(
            format_version=MANIFEST_FORMAT_VERSION,
            engine_pack_version=_EP_RELEASE_VERSION,
            portable_release_version=_EP_RELEASE_VERSION,
            source_commit="7c2764bae599f3e173f8bf63463baf961013650a",
            source_commit_short="7c2764b",
            archive_filename="test.zip",
            archive_crc32="123",
            archive_sha256="a" * 64,
            engines=[],
        )
        errors = validate_manifest(m)
        assert any("crc32" in e.lower() for e in errors)

    def test_get_engine_pack_info(self) -> None:
        """get_engine_pack_info 返回内置信息。"""
        from blc_portable.engine_pack.manifest import get_engine_pack_info

        info = get_engine_pack_info()
        assert "filename" in info
        assert "crc32" in info
        assert "expected_engine_ids" in info
        assert len(info["expected_engine_ids"]) == 4


# ── CRC32 测试 ────────────────────────────────────────────────


class TestCRC32:
    """CRC32 流式计算测试。"""

    def test_crc32_uppercase_hex(self, tmp_path: Path) -> None:
        """CRC32 输出应为 8 位大写十六进制。"""
        from blc_portable.engine_pack.installer import compute_crc32

        p = tmp_path / "test.bin"
        p.write_bytes(b"test")
        result = compute_crc32(p)
        assert len(result) == 8
        assert result == result.upper()
        assert all(c in "0123456789ABCDEF" for c in result)

    def test_crc32_empty_file(self, tmp_path: Path) -> None:
        """空文件 CRC32 应为 00000000。"""
        from blc_portable.engine_pack.installer import compute_crc32

        p = tmp_path / "empty.bin"
        p.write_bytes(b"")
        assert compute_crc32(p) == "00000000"


# ── Engine Pack 安装测试 ─────────────────────────────────────


class TestEnginePackInstall:
    """本地 Engine Pack 安装测试。"""

    def test_find_local_engine_pack(self, tmp_app_root: Path, fixture_engine_pack: Path) -> None:
        """在 app_root 下能找到 Engine Pack。"""
        from blc_portable.engine_pack.installer import find_local_engine_pack

        dest = tmp_app_root / fixture_engine_pack.name
        shutil.copy2(str(fixture_engine_pack), str(dest))

        result = find_local_engine_pack(tmp_app_root, fixture_engine_pack.name)
        assert result is not None
        assert result.name == fixture_engine_pack.name

    def test_find_local_not_found(self, tmp_app_root: Path) -> None:
        """找不到时应返回 None。"""
        from blc_portable.engine_pack.installer import find_local_engine_pack

        result = find_local_engine_pack(tmp_app_root, "nonexistent.zip")
        assert result is None

    def test_crc32_match_install(self, tmp_app_root: Path, fixture_engine_pack: Path) -> None:
        """CRC32 匹配时应成功安装，网络请求为 0。"""
        from blc_portable.engine_pack.installer import compute_crc32, compute_sha256, install_from_engine_pack

        dest = tmp_app_root / fixture_engine_pack.name
        shutil.copy2(str(fixture_engine_pack), str(dest))

        crc32_val = compute_crc32(dest)
        sha256_val = compute_sha256(dest)

        result = install_from_engine_pack(
            tmp_app_root,
            dest,
            expected_crc32=crc32_val,
            expected_sha256=sha256_val,
            expected_version=_EP_RELEASE_VERSION,
        )

        assert result["source"] == "engine_pack"
        assert result["network_requests"] == 0
        assert len(result["engines"]) == 4

        models_dir = tmp_app_root / "models"
        assert (models_dir / "whisper").exists()
        assert (models_dir / "paraformer").exists()
        assert (models_dir / "sensevoice").exists()
        assert (models_dir / "funasr_nano").exists()

        installed = models_dir / "engine-pack-installed.json"
        assert installed.exists()
        info = json.loads(installed.read_text(encoding="utf-8"))
        assert info["engine_pack_version"] == _EP_RELEASE_VERSION

    def test_user_supplied_pack_without_embedded_digests(self, tmp_app_root: Path, fixture_engine_pack: Path) -> None:
        """No-pack launchers accept local packs after complete internal-manifest verification."""
        from blc_portable.engine_pack.installer import install_from_engine_pack

        dest = tmp_app_root / fixture_engine_pack.name
        shutil.copy2(str(fixture_engine_pack), str(dest))

        result = install_from_engine_pack(
            tmp_app_root,
            dest,
            expected_crc32="",
            expected_sha256="",
            expected_version=_EP_RELEASE_VERSION,
        )

        assert result["source"] == "engine_pack"
        assert result["network_requests"] == 0

    def test_crc32_mismatch_raises(self, tmp_app_root: Path, fixture_engine_pack: Path) -> None:
        """CRC32 不匹配时应抛出 RuntimeError。"""
        from blc_portable.engine_pack.installer import compute_sha256, install_from_engine_pack

        dest = tmp_app_root / fixture_engine_pack.name
        shutil.copy2(str(fixture_engine_pack), str(dest))
        sha256_val = compute_sha256(dest)

        with pytest.raises(RuntimeError, match="CRC32 mismatch"):
            install_from_engine_pack(
                tmp_app_root,
                dest,
                expected_crc32="DEADBEEF",
                expected_sha256=sha256_val,
                expected_version=_EP_RELEASE_VERSION,
            )

    def test_bad_crc32_does_not_install_models(self, tmp_app_root: Path, fixture_engine_pack: Path) -> None:
        """CRC32 失败时不应安装任何模型。"""
        from blc_portable.engine_pack.installer import compute_sha256, install_from_engine_pack

        dest = tmp_app_root / fixture_engine_pack.name
        shutil.copy2(str(fixture_engine_pack), str(dest))
        sha256_val = compute_sha256(dest)

        try:
            install_from_engine_pack(
                tmp_app_root,
                dest,
                expected_crc32="DEADBEEF",
                expected_sha256=sha256_val,
                expected_version=_EP_RELEASE_VERSION,
            )
        except RuntimeError:
            pass

        models_dir = tmp_app_root / "models"
        assert not (models_dir / "whisper" / "model.bin").exists()


# ── 已安装模型检测 ───────────────────────────────────────────


class TestCheckInstalledModels:
    """已安装模型检测测试。"""

    def test_not_installed(self, tmp_app_root: Path) -> None:
        """未安装时返回 False。"""
        from blc_portable.engine_pack.installer import check_installed_models

        ok, _ = check_installed_models(tmp_app_root / "models", _EP_RELEASE_VERSION)
        assert not ok

    def test_version_mismatch(self, tmp_app_root: Path) -> None:
        """版本不匹配时返回 False。"""
        from blc_portable.engine_pack.installer import check_installed_models

        models_dir = tmp_app_root / "models"
        for eng in ["whisper", "paraformer", "sensevoice", "funasr_nano"]:
            (models_dir / eng).mkdir(exist_ok=True)
            (models_dir / eng / "model.bin").write_bytes(b"test")

        (models_dir / "engine-pack-installed.json").write_text(
            json.dumps(
                {
                    "engine_pack_version": "0.1.13.0-alpha",
                    "engines_installed": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
                }
            ),
            encoding="utf-8",
        )

        ok1, _ = check_installed_models(models_dir, _EP_RELEASE_VERSION)
        assert not ok1

    def test_installed_and_valid(self, tmp_app_root: Path) -> None:
        """正确安装时返回 True。"""
        from blc_portable.engine_pack.installer import check_installed_models

        models_dir = tmp_app_root / "models"
        for eng in ["whisper", "paraformer", "sensevoice", "funasr_nano"]:
            (models_dir / eng).mkdir(exist_ok=True)
            (models_dir / eng / "model.bin").write_bytes(b"test")

        (models_dir / "engine-pack-installed.json").write_text(
            json.dumps(
                {
                    "engine_pack_version": _EP_RELEASE_VERSION,
                    "engines_installed": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
                }
            ),
            encoding="utf-8",
        )

        ok2, _ = check_installed_models(models_dir, _EP_RELEASE_VERSION)
        assert ok2


# ── Zip Slip 防护 ─────────────────────────────────────────────


class TestZipSlip:
    """安全解压测试。"""

    def test_reject_absolute_path(self, tmp_path: Path) -> None:
        """包含绝对路径的 ZIP 应被拒绝。"""
        from blc_portable.engine_pack.installer import _safe_extract

        zip_path = tmp_path / "bad.zip"
        target = tmp_path / "out"
        target.mkdir(exist_ok=True)

        with zipfile.ZipFile(str(zip_path), "w") as zf:
            zf.writestr("/etc/passwd", "bad")

        with pytest.raises(RuntimeError, match="绝对路径"):
            _safe_extract(zip_path, target)

    def test_reject_parent_traversal(self, tmp_path: Path) -> None:
        """包含 .. 的 ZIP 应被拒绝。"""
        from blc_portable.engine_pack.installer import _safe_extract

        zip_path = tmp_path / "bad.zip"
        target = tmp_path / "out"
        target.mkdir(exist_ok=True)

        with zipfile.ZipFile(str(zip_path), "w") as zf:
            zf.writestr("../escape.txt", "bad")

        with pytest.raises(RuntimeError, match=r"\.\."):
            _safe_extract(zip_path, target)


# ── 哈希函数测试 ──────────────────────────────────────────────


class TestHashFunctions:
    """哈希函数测试。"""

    def test_sha256_known_value(self, tmp_path: Path) -> None:
        """SHA-256 已知值测试。"""
        from blc_portable.engine_pack.installer import compute_sha256

        p = tmp_path / "test.bin"
        p.write_bytes(b"hello")
        expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        assert compute_sha256(p) == expected

    def test_crc32_known_value(self, tmp_path: Path) -> None:
        """CRC32 已知值 (123456789)。"""
        from blc_portable.engine_pack.installer import compute_crc32

        p = tmp_path / "crc32_test.bin"
        p.write_bytes(b"123456789")
        result = compute_crc32(p)
        assert result == "CBF43926"
