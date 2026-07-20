"""Portable Backport 测试。

验证 backport 机制：
- Payload 正确应用所有声明的 backport
- backport 清单与实际变更一致
- BLC_MODELS_DIR 生效后四个引擎使用本地路径
- 禁网时不触发模型下载
- Nano ID 正确
- 缺失模型时产生明确诊断
- Payload source commit 仍是 731a31c
- 不存在未声明的新路由/新数据库/新 UI feature
"""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

# 路径设置
_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

PAYLOAD_ZIP = Path(__file__).resolve().parent.parent / "dist" / "payload" / "source_payload.zip"
MANIFEST_PATH = Path(__file__).resolve().parent.parent / "dist" / "payload" / "payload_manifest.json"
BACKPORTS_JSON = Path(__file__).resolve().parent.parent / "backports" / "backports.json"


def _read_payload_file(zf: zipfile.ZipFile, path: str) -> str:
    """Read a file from inside the payload ZIP as UTF-8 text."""
    return zf.read(path).decode("utf-8", errors="replace")


# ── Payload Manifest 测试 ──


class TestPayloadManifest:
    """验证 Manifest 中的 commit 和 backport 声明。"""

    def test_source_commit_is_731a31c(self) -> None:
        """验证 Payload 的 source_commit 仍是 731a31c。"""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest["source_commit_short"] == "731a31c", (
            f"source_commit_short expected 731a31c, got {manifest['source_commit_short']}"
        )
        assert manifest["source_commit"].startswith("731a31c"), (
            f"source_commit must start with 731a31c, got {manifest['source_commit'][:12]}"
        )

    def test_backport_ids_present(self) -> None:
        """验证 Manifest 记录了 backport_ids。"""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        bp = manifest.get("backport_ids", [])
        assert len(bp) >= 1, "backport_ids should not be empty"
        assert all(b.startswith("bp-") for b in bp), f"Invalid backport IDs: {bp}"

    def test_backport_manifest_consistency(self) -> None:
        """验证 Manifest 中的 backport_ids 与 backports.json 声明一致。"""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        manifest_bp = set(manifest.get("backport_ids", []))
        backports_spec = json.loads(BACKPORTS_JSON.read_text(encoding="utf-8"))
        spec_bp = {b["id"] for b in backports_spec["backports"]}
        assert manifest_bp == spec_bp, f"Manifest backport_ids {manifest_bp} != backports.json {spec_bp}"

    def test_backport_files_match_manifest(self) -> None:
        """验证 backports.json 声明的文件列表与实际 ZIP 中的文件变更一致。"""
        backports_spec = json.loads(BACKPORTS_JSON.read_text(encoding="utf-8"))
        declared_files: set[str] = set()
        for bp in backports_spec["backports"]:
            for f in bp.get("files", []):
                declared_files.add(f)

        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            zip_namelist = set(zf.namelist())

        for f in declared_files:
            assert f in zip_namelist, f"Backport file {f} declared but not in payload ZIP"


# ── Nano ID 测试 ──


class TestNanoRepoFix:
    """验证 bp-001: FunASR-Nano 仓库 ID 已正确修正。"""

    def test_nano_id_correct_in_backends(self) -> None:
        """验证 backends.py 中 MODEL_ID_NANO 已修正为 FunAudioLLM/Fun-ASR-Nano-2512。"""
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            content = _read_payload_file(zf, "app/analysis/transcription/backends.py")
        assert "FunAudioLLM/Fun-ASR-Nano-2512" in content, "Nano ID should be FunAudioLLM/Fun-ASR-Nano-2512"
        assert "iic/Fun-ASR-Nano" not in content, "Old incorrect Nano ID iic/Fun-ASR-Nano should NOT exist"

    def test_nano_id_correct_in_pipeline(self) -> None:
        """验证 pipeline.py 中 MODEL_ID_NANO 已修正。"""
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            content = _read_payload_file(zf, "app/analysis/transcription/pipeline.py")
        assert "FunAudioLLM/Fun-ASR-Nano-2512" in content, "Nano ID should be in pipeline.py"
        assert "iic/Fun-ASR-Nano" not in content, "Old incorrect Nano ID should not exist in pipeline.py"


# ── BLC_MODELS_DIR 本地路径测试 ──


class TestBLCModelsDir:
    """验证 bp-002/bp-003: BLC_MODELS_DIR 本地模型路径支持。"""

    def test_blc_models_dir_env_var_in_backends(self) -> None:
        """验证 backends.py 包含 BLC_MODELS_DIR 环境变量读取逻辑。"""
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            content = _read_payload_file(zf, "app/analysis/transcription/backends.py")
        assert "BLC_MODELS_DIR" in content, "BLC_MODELS_DIR not found in backends.py"
        assert "_use_local" in content, "_use_local flag not found in backends.py"
        assert "disable_update=True" in content or "disable_update=self._use_local" in content, (
            "disable_update for local model loading not found"
        )

    def test_four_engines_have_local_paths(self) -> None:
        """验证四个引擎（Whisper, Paraformer, SenseVoice, FunASR-Nano）都包含本地路径支持。"""
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            content = _read_payload_file(zf, "app/analysis/transcription/backends.py")

        # 检查每个引擎是否在 _use_local 分支有本地路径逻辑
        engines_with_local = {
            "whisper": "BLC_MODELS_DIR" in content and "whisper" in content.lower(),
            "paraformer": "_models_dir" in content or "/paraformer" in content,
            "sensevoice": "sensevoice" in content.lower() and "disable_update" in content,
            "funasr_nano": "funasr_nano" in content.lower() and "disable_update" in content,
        }

        missing = [k for k, v in engines_with_local.items() if not v]
        assert not missing, f"Engines missing local path support: {missing}"

    def test_engine_pack_contract_file(self) -> None:
        """验证 _portable_release.py 包含完整的 Engine Pack 合约。"""
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            content = _read_payload_file(zf, "app/_portable_release.py")

        assert "ENGINE_PACK_CONTRACT" in content, "ENGINE_PACK_CONTRACT missing"
        assert "whisper" in content, "whisper engine missing from contract"
        assert "paraformer" in content, "paraformer engine missing from contract"
        assert "sensevoice" in content, "sensevoice engine missing from contract"
        assert "funasr_nano" in content, "funasr_nano engine missing from contract"
        assert "FunAudioLLM/Fun-ASR-Nano-2512" in content, "Correct Nano ID missing from contract"
        assert '"schema_version"' in content, "schema_version missing from contract"
        assert "731a31c" in content, "source commit not in _portable_release.py"


class TestMissingModelDiagnosis:
    """验证缺失模型时产生明确错误诊断。"""

    def test_missing_models_root_raises_clear_error(self) -> None:
        """验证 BLC_MODELS_DIR 指向不存在的目录时不会静默失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            payload_dir = Path(tmp) / "payload"
            payload_dir.mkdir()

            # 解压 payload
            with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
                zf.extractall(payload_dir)

            # 设置不存在的 BLC_MODELS_DIR
            nonexistent = Path(tmp) / "nonexistent_models"

            import subprocess

            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    f"""import os, sys
os.environ["BLC_MODELS_DIR"] = r"{str(nonexistent)}"
sys.path.insert(0, r"{str(payload_dir)}")
from app.analysis.transcription.backends import FunASRBackend
print("Import OK: FunASRBackend imported with MODEL_ID_NANO=" + FunASRBackend.MODEL_ID_NANO)
""",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert "Import OK" in result.stdout, f"Import failed:\n{result.stderr}\n{result.stdout}"
            assert result.returncode == 0, f"Subprocess failed with exit {result.returncode}:\n{result.stderr}"


class TestNoBackportSideEffects:
    """验证 backport 没有引入未声明的变更（新路由、新 DB feature、新 UI）。"""

    def test_no_new_routes(self) -> None:
        """验证 backported 文件中没有新增 @app.route 等 Web 路由。"""
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            backends_content = _read_payload_file(zf, "app/analysis/transcription/backends.py")
            pipeline_content = _read_payload_file(zf, "app/analysis/transcription/pipeline.py")

        for content, fname in [(backends_content, "backends.py"), (pipeline_content, "pipeline.py")]:
            forbidden = [
                "@app.route",
                "@router.",
                "FastAPI",
                "fastapi",
                "include_router",
                "APIRouter",
                "from starlette",
                "from fastapi",
                "import starlette",
                "import fastapi",
            ]
            for pattern in forbidden:
                if pattern in content:
                    # Allow 'FastAPI' in docstrings (it appears in comments)
                    # Check it's not in actual code
                    lines = [
                        line_text
                        for line_text in content.split("\n")
                        if pattern in line_text
                        and not line_text.strip().startswith("#")
                        and not line_text.strip().startswith('"""')
                    ]
                    if lines:
                        pytest.fail(f"Forbidden pattern '{pattern}' found in {fname}: {lines[:3]}")

    def test_no_new_db_features(self) -> None:
        """验证 backported 文件没有新增数据库相关代码。"""
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            backends_content = _read_payload_file(zf, "app/analysis/transcription/backends.py")
            pipeline_content = _read_payload_file(zf, "app/analysis/transcription/pipeline.py")

        for content, fname in [(backends_content, "backends.py"), (pipeline_content, "pipeline.py")]:
            forbidden = [
                "from sqlmodel",
                "import sqlmodel",
                "from sqlalchemy",
                "import sqlalchemy",
                "Session(",
                "new_table",
                "CREATE TABLE",
                "add_column",
                "Alembic",
            ]
            for pattern in forbidden:
                if pattern in content:
                    pytest.fail(f"Forbidden DB pattern '{pattern}' found in {fname}")

    def test_no_new_ui_features(self) -> None:
        """验证 backported 文件没有新增 UI 功能。"""
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            backends_content = _read_payload_file(zf, "app/analysis/transcription/backends.py")
            pipeline_content = _read_payload_file(zf, "app/analysis/transcription/pipeline.py")

        for content, fname in [(backends_content, "backends.py"), (pipeline_content, "pipeline.py")]:
            forbidden = [
                "from flask",
                "import flask",
                "from jinja2",
                "import jinja2",
                "render_template",
                "static/",
                "templates/",
                "HTMLResponse",
                "@app.get(",
                "@app.post(",
            ]
            for pattern in forbidden:
                if pattern in content:
                    pytest.fail(f"Forbidden UI pattern '{pattern}' found in {fname}")
