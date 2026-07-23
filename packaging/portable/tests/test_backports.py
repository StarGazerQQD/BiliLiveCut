"""Portable 最新源码基线与历史 Backport 退役回归测试。"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

PORTABLE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PORTABLE_DIR.parent.parent
SRC_DIR = PORTABLE_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

PAYLOAD_ZIP = PORTABLE_DIR / "dist" / "payload" / "source_payload.zip"
MANIFEST_PATH = PORTABLE_DIR / "dist" / "payload" / "payload_manifest.json"
BACKPORTS_JSON = PORTABLE_DIR / "backports" / "backports.json"
SOURCE_COMMIT_FULL = "0fe24a5f050c7110b2214570ac165d828f5f363c"
SOURCE_COMMIT_SHORT = "0fe24a5"


def _load_json(path: Path) -> dict[str, Any]:
    """加载 UTF-8 JSON 对象。"""
    return json.loads(path.read_text(encoding="utf-8"))


def _read_payload_file(zf: zipfile.ZipFile, path: str) -> str:
    """读取 Payload ZIP 内的 UTF-8 文本。"""
    return zf.read(path).decode("utf-8")


def _read_git_file(path: str) -> str:
    """读取固定源码基线中的文本文件。"""
    result = subprocess.run(
        ["git", "show", f"{SOURCE_COMMIT_FULL}:{path}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8")


class TestPayloadBaselineIdentity:
    """验证 Payload 身份已切换到当前源码基线且不再叠加 Backport。"""

    def test_manifest_uses_current_source_commit(self) -> None:
        manifest = _load_json(MANIFEST_PATH)
        assert manifest["source_commit"] == SOURCE_COMMIT_FULL
        assert manifest["source_commit_short"] == SOURCE_COMMIT_SHORT
        assert manifest["core_source_commit"] == SOURCE_COMMIT_FULL
        assert manifest["core_source_commit_short"] == SOURCE_COMMIT_SHORT

    def test_backport_lists_are_empty(self) -> None:
        manifest = _load_json(MANIFEST_PATH)
        specification = _load_json(BACKPORTS_JSON)
        assert manifest["applied_backports"] == []
        assert manifest["backport_ids"] == []
        assert specification == {
            "format_version": 1,
            "source_commit": SOURCE_COMMIT_FULL,
            "backports": [],
        }

    def test_obsolete_backport_patcher_is_removed(self) -> None:
        patcher = PORTABLE_DIR / "src" / "blc_portable" / "payload" / "backport_patcher.py"
        assert not patcher.exists()

    def test_builder_does_not_accept_an_alternate_source_commit(self) -> None:
        from blc_portable.payload.builder import build_payload

        assert "source_commit" not in inspect.signature(build_payload).parameters

    def test_release_metadata_is_overlaid_with_build_identity(self) -> None:
        manifest = _load_json(MANIFEST_PATH)
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            content = _read_payload_file(zf, "app/_portable_release.py")

        assert f'SOURCE_COMMIT: str = "{SOURCE_COMMIT_FULL}"' in content
        assert f'SOURCE_COMMIT_SHORT: str = "{SOURCE_COMMIT_SHORT}"' in content
        assert f'BUILDER_COMMIT: str = "{manifest["builder_commit"]}"' in content


class TestLatestBaselineBehavior:
    """验证历史修复已原生存在于新基线。"""

    def test_nano_repository_id_is_current(self) -> None:
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            backends = _read_payload_file(zf, "app/analysis/transcription/backends.py")
            pipeline = _read_payload_file(zf, "app/analysis/transcription/pipeline.py")

        assert "FunAudioLLM/Fun-ASR-Nano-2512" in backends
        assert "iic/Fun-ASR-Nano" not in backends
        assert "FunASRBackend" in pipeline
        assert "iic/Fun-ASR-Nano" not in pipeline

    def test_local_model_support_is_present(self) -> None:
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            content = _read_payload_file(zf, "app/analysis/transcription/backends.py")

        assert "BLC_MODELS_DIR" in content
        assert "_use_local_models" in content
        assert "disable_update=True" in content
        for engine in ("whisper", "paraformer", "sensevoice", "funasr_nano"):
            assert engine in content.lower()

    def test_csrf_origin_validation_is_present(self) -> None:
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            content = _read_payload_file(zf, "app/web/main.py")

        assert "def _parse_origin(" in content
        assert 'scheme not in ("http", "https")' in content
        assert "origin_port_for_compare" in content

    def test_payload_business_files_match_fixed_baseline(self) -> None:
        business_files = (
            "app/cli.py",
            "app/analysis/transcription/backends.py",
            "app/analysis/transcription/pipeline.py",
            "app/web/login_handler.py",
            "app/web/main.py",
            "app/web/static/js/review.js",
        )
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            for path in business_files:
                payload_text = _read_payload_file(zf, path).replace("\r\n", "\n")
                baseline_text = _read_git_file(path).replace("\r\n", "\n")
                assert payload_text == baseline_text, path

    def test_missing_models_root_does_not_break_backend_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload_dir = Path(tmp) / "payload"
            payload_dir.mkdir()
            with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
                zf.extractall(payload_dir)

            nonexistent = Path(tmp) / "nonexistent_models"
            script = (
                "import os, sys\n"
                f'os.environ["BLC_MODELS_DIR"] = r"{nonexistent}"\n'
                f'sys.path.insert(0, r"{payload_dir}")\n'
                "from app.analysis.transcription.backends import FunASRBackend\n"
                'print("Import OK: " + FunASRBackend.MODEL_ID_NANO)\n'
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        assert result.returncode == 0, result.stderr
        assert "Import OK: FunAudioLLM/Fun-ASR-Nano-2512" in result.stdout
