"""模型目录测试 — 验证 model_sources.lock.json 为唯一权威来源。

检查: 无重复定义、FunASR-Nano 使用正确仓库、所有模型有 resolved_revision 等。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "packaging" / "portable" / "config"

sys.path.insert(0, str(CONFIG_DIR))


def _load_catalog() -> dict:
    p = CONFIG_DIR / "model_sources.lock.json"
    return json.loads(p.read_text(encoding="utf-8"))


class TestModelCatalogIsSingleSource:
    """验证模型目录为唯一权威来源。"""

    def test_catalog_exists_and_valid(self) -> None:
        """验证 catalog JSON 存在且格式有效。"""
        cat = _load_catalog()
        assert cat["schema_version"] == 2
        assert "engines" in cat
        assert len(cat["engines"]) >= 4

    def test_funasr_nano_uses_correct_repository(self) -> None:
        """Fun-ASR-Nano 不得使用错误的 iic/Fun-ASR-Nano。"""
        from model_catalog import get_engine_by_id

        engine = get_engine_by_id("funasr_nano")
        assert engine is not None, "未找到 funasr_nano 引擎"
        assert "FunAudioLLM/Fun-ASR-Nano-2512" in engine.repository, (
            f"funasr_nano 使用错误仓库: {engine.repository}，应使用 FunAudioLLM/Fun-ASR-Nano-2512"
        )

    def test_sensevoice_has_valid_repository(self) -> None:
        """SenseVoiceSmall 仓库必须正确。"""
        from model_catalog import get_engine_by_id

        engine = get_engine_by_id("sensevoice")
        assert engine is not None
        assert "SenseVoiceSmall" in engine.repository

    def test_paraformer_uses_full_repository_id(self) -> None:
        """Paraformer-zh 必须使用完整仓库 ID，而非缩写。"""
        from model_catalog import get_engine_by_id

        engine = get_engine_by_id("paraformer")
        assert engine is not None
        assert engine.repository.startswith("iic/"), (
            f"paraformer repository 格式无效: {engine.repository}"
        )
        assert "/" in engine.repository

    def test_whisper_uses_correct_repository(self) -> None:
        """Whisper 仓库不可变。"""
        from model_catalog import get_engine_by_id

        engine = get_engine_by_id("whisper")
        assert engine is not None
        assert "mobiuslabsgmbh/faster-whisper-large-v3-turbo" in engine.repository

    def test_all_engines_have_resolved_revision(self) -> None:
        """所有引擎必须有 resolved_revision。"""
        from model_catalog import load_engines

        engines = load_engines()
        for e in engines:
            assert e.resolved_revision, (
                f"引擎 {e.engine_id} 的 resolved_revision 为空"
            )

    def test_no_duplicate_engine_ids(self) -> None:
        """引擎 ID 必须唯一。"""
        from model_catalog import load_engines

        engines = load_engines()
        ids = [e.engine_id for e in engines]
        assert len(ids) == len(set(ids)), f"引擎 ID 重复: {ids}"

    def test_no_duplicate_target_paths(self) -> None:
        """target_path 必须唯一。"""
        from model_catalog import load_engines

        engines = load_engines()
        paths = [e.target_path for e in engines]
        assert len(paths) == len(set(paths)), f"target_path 重复: {paths}"

    def test_catalog_validation_no_errors(self) -> None:
        """validate_catalog() 返回空列表。"""
        from model_catalog import validate_catalog

        errors = validate_catalog()
        assert not errors, f"模型目录校验失败: {errors}"

    def test_engine_pack_version_matches_release(self) -> None:
        """Engine Pack 版本与 release_version 一致。"""
        from model_catalog import get_engine_pack_version
        from version_loader import get_version

        assert get_engine_pack_version() == get_version(), (
            "engine_pack_version 与 release_version 不一致"
        )

    def test_required_files_non_empty(self) -> None:
        """所有引擎 required_files 非空。"""
        from model_catalog import load_engines

        engines = load_engines()
        for e in engines:
            assert e.required_files, (
                f"引擎 {e.engine_id} required_files 为空"
            )


class TestModelCatalogIntegration:
    """验证各模块使用同一个模型目录。"""

    def test_online_and_offline_same_catalog(self) -> None:
        """在线下载器和离线 builder 使用同一目录。"""
        from model_catalog import load_engines

        engines = load_engines()
        engine_ids = {e.engine_id for e in engines}
        expected = {"whisper", "paraformer", "sensevoice", "funasr_nano"}
        assert engine_ids == expected, f"引擎 ID 不一致: {engine_ids} != {expected}"

    def test_builder_uses_catalog_not_hardcoded(self) -> None:
        """验证 builder.py 不再有硬编码 ENGINES 列表。"""
        builder_path = (
            REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "engine_pack" / "builder.py"
        )
        content = builder_path.read_text(encoding="utf-8")
        # 不应再出现硬编码的 ENGINES: list 定义
        assert "ENGINES: list" not in content, "builder.py 仍有硬编码 ENGINES 常量"
        # 应有模型目录导入
        assert "model_catalog" in content, "builder.py 未导入 model_catalog"

    def test_manifest_uses_catalog_not_hardcoded(self) -> None:
        """验证 manifest.py 不再有硬编码 ENGINES。"""
        manifest_path = (
            REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "engine_pack" / "manifest.py"
        )
        content = manifest_path.read_text(encoding="utf-8")
        assert "ENGINES: list" not in content, "manifest.py 仍有硬编码 ENGINES"
        assert "model_catalog" in content, "manifest.py 未导入 model_catalog"
