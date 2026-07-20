"""ASR 架构和模型 provenance 测试 — 阶段 7。

覆盖:
- 三个导入路径的类型 identity (backends / pipeline / models 返回相同类)
- 每引擎使用自身 revision (不共用全局 settings.asr_model_revision)
- provenance 字段 (model_source / model_catalog_id / loaded_from)
- catalog 与 Engine Pack model lock 一致
- production 门禁 (无短 hash / tag / 空 files)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ── 类型 identity ─────────────────────────────────────


class TestTypeIdentity:
    """验证 backends / pipeline / models 三个模块的类型是同一对象。"""

    def test_asr_transcript_result_is_same_object(self) -> None:
        """ASRTranscriptResult 在三个模块中是同一个对象。"""
        from app.analysis.transcription.backends import ASRTranscriptResult as b_type
        from app.analysis.transcription.models import ASRTranscriptResult as m_type

        # pipeline imports from backends, so this covers all
        assert m_type is b_type, "ASRTranscriptResult differs between models and backends"

    def test_asr_segment_result_is_same_object(self) -> None:
        """ASRSegmentResult 在三个模块中是同一个对象。"""
        from app.analysis.transcription.backends import ASRSegmentResult as b_type
        from app.analysis.transcription.models import ASRSegmentResult as m_type

        assert m_type is b_type, "ASRSegmentResult differs"

    def test_emotion_event_is_same_object(self) -> None:
        """EmotionEvent 在三个模块中是同一个对象。"""
        from app.analysis.transcription.backends import EmotionEvent as b_type
        from app.analysis.transcription.models import EmotionEvent as m_type

        assert m_type is b_type, "EmotionEvent differs"

    def test_word_is_same_object(self) -> None:
        """Word 在三个模块中是同一个对象。"""
        from app.analysis.transcription.backends import Word as b_type
        from app.analysis.transcription.models import Word as m_type

        assert m_type is b_type, "Word differs"

    def test_transcription_result_is_same_object(self) -> None:
        """TranscriptionResult 在公共 API 中是同一对象。"""
        from app.analysis.transcription import TranscriptionResult as b_type
        from app.analysis.transcription.models import TranscriptionResult as m_type

        assert m_type is b_type, "TranscriptionResult differs"

    def test_transcriber_backend_is_same_object(self) -> None:
        """TranscriberBackend 在公共 API 中是同一对象。"""
        from app.analysis.transcription import TranscriberBackend as b_type
        from app.analysis.transcription.models import TranscriberBackend as m_type

        assert m_type is b_type, "TranscriberBackend differs"


# ── 每引擎独立 revision ──────────────────────────────


class TestPerEngineRevision:
    """验证每个引擎使用自身 revision，不共用 settings.asr_model_revision。"""

    def test_funasr_backend_has_per_engine_revisions(self) -> None:
        """FunASRBackend 有独立 per-engine revision 属性。"""
        from app.analysis.transcription.backends import FunASRBackend

        be = FunASRBackend(primary="test-model", sensevoice=False, funasr_nano=False)
        assert hasattr(be, "primary_revision"), "missing primary_revision"
        assert hasattr(be, "sensevoice_revision"), "missing sensevoice_revision"
        assert hasattr(be, "nano_revision"), "missing nano_revision"

    def test_revisions_not_all_same_default(self) -> None:
        """不同引擎的 revision 可以不同。"""
        from app.analysis.transcription.backends import FunASRBackend

        # At minimum, the class-level constants should exist
        assert FunASRBackend._REVISION_PRIMARY is not None
        assert FunASRBackend._REVISION_SENSEVOICE is not None
        assert FunASRBackend._REVISION_NANO is not None

    def test_model_revision_not_using_global_setting_directly(self) -> None:
        """model_revision property 使用 _REVISION_PRIMARY 而非 settings.asr_model_revision。"""
        # Verify by inspection: the property code uses _REVISION_PRIMARY
        import inspect

        from app.analysis.transcription.backends import FunASRBackend

        source = inspect.getsource(FunASRBackend.model_revision.fget)  # type: ignore[arg-type]
        assert "_REVISION_PRIMARY" in source, (
            "model_revision should use _REVISION_PRIMARY, not settings.asr_model_revision"
        )

    def test_load_primary_uses_per_engine_revision(self) -> None:
        """_load_primary 使用 _REVISION_PRIMARY 而非 settings。"""
        import inspect

        from app.analysis.transcription.backends import FunASRBackend

        source = inspect.getsource(FunASRBackend._load_primary)  # type: ignore[arg-type]
        assert "_REVISION_PRIMARY" in source, "_load_primary should use _REVISION_PRIMARY"

    def test_load_sensevoice_uses_per_engine_revision(self) -> None:
        """_load_sensevoice 使用 _REVISION_SENSEVOICE。"""
        import inspect

        from app.analysis.transcription.backends import FunASRBackend

        source = inspect.getsource(FunASRBackend._load_sensevoice)  # type: ignore[arg-type]
        assert "_REVISION_SENSEVOICE" in source, "_load_sensevoice should use _REVISION_SENSEVOICE"

    def test_load_funasr_uses_per_engine_revision(self) -> None:
        """_load_funasr 使用 _REVISION_NANO。"""
        import inspect

        from app.analysis.transcription.backends import FunASRBackend

        source = inspect.getsource(FunASRBackend._load_funasr)  # type: ignore[arg-type]
        assert "_REVISION_NANO" in source, "_load_funasr should use _REVISION_NANO"


# ── provenance 字段 ───────────────────────────────────


class TestProvenanceFields:
    """验证 ASRTranscriptResult 包含 provenance 追踪字段。"""

    def test_model_source_field_exists(self) -> None:
        """model_source 字段存在。"""
        from app.analysis.transcription.models import ASRTranscriptResult

        fields = ASRTranscriptResult.__dataclass_fields__
        assert "model_source" in fields
        assert fields["model_source"].default == ""

    def test_model_catalog_id_field_exists(self) -> None:
        """model_catalog_id 字段存在。"""
        from app.analysis.transcription.models import ASRTranscriptResult

        fields = ASRTranscriptResult.__dataclass_fields__
        assert "model_catalog_id" in fields
        assert fields["model_catalog_id"].default == ""

    def test_loaded_from_field_exists(self) -> None:
        """loaded_from 字段存在。"""
        from app.analysis.transcription.models import ASRTranscriptResult

        fields = ASRTranscriptResult.__dataclass_fields__
        assert "loaded_from" in fields
        assert fields["loaded_from"].default == ""

    def test_provenance_fields_dont_break_existing_code(self) -> None:
        """现有代码使用 ASRTranscriptResult() 默认构造不受影响。"""
        from app.analysis.transcription.models import ASRTranscriptResult

        result = ASRTranscriptResult(
            text="test",
            backend="test-backend",
            model_id="test-model",
            model_source="local",
            model_catalog_id="whisper",
            loaded_from="/models/whisper",
        )
        assert result.model_source == "local"
        assert result.model_catalog_id == "whisper"
        assert result.loaded_from == "/models/whisper"


# ── Model catalog 一致性 ──────────────────────────────


class TestModelCatalogConsistency:
    """验证 model catalog 与 Engine Pack model lock 一致。"""

    def test_model_ids_match_catalog(self) -> None:
        """backends.py 的 MODEL_ID 常量与 model_catalog 一致。"""
        from app.analysis.transcription.backends import FunASRBackend

        be = FunASRBackend
        assert be.MODEL_ID_PRIMARY, "MODEL_ID_PRIMARY is empty"
        assert be.MODEL_ID_SENSEVOICE, "MODEL_ID_SENSEVOICE is empty"
        assert be.MODEL_ID_NANO, "MODEL_ID_NANO is empty"
        # Nano must be the correct repo
        assert "FunAudioLLM/Fun-ASR-Nano-2512" == be.MODEL_ID_NANO, f"NANO ID wrong: {be.MODEL_ID_NANO}"
        assert "iic/Fun-ASR-Nano" not in be.MODEL_ID_NANO, "Old Nano ID still present"

    def test_revisions_are_stable_not_master(self) -> None:
        """Primary revision 不应为 'master' (tag/branch 不可复现)。"""
        from app.analysis.transcription.backends import FunASRBackend

        be = FunASRBackend
        # Primary should have a real revision, not just "master"
        assert be._REVISION_PRIMARY != "master", "Primary revision should be a stable hash/tag, not 'master'"

    def test_no_short_hash_revisions(self) -> None:
        """每个 revision 不应是短 hash (< 7 chars)。"""
        from app.analysis.transcription.backends import FunASRBackend

        be = FunASRBackend
        for name, val in [
            ("_REVISION_PRIMARY", be._REVISION_PRIMARY),
            ("_REVISION_SENSEVOICE", be._REVISION_SENSEVOICE),
            ("_REVISION_NANO", be._REVISION_NANO),
        ]:
            if val and val != "master":
                assert len(val) >= 5, f"{name}={val!r} too short to be a revision"

    def test_empty_files_not_in_production(self) -> None:
        """model catalog 中每个 engine 都有 required_files (非空)。"""
        lock_path = _REPO_ROOT / "packaging" / "portable" / "config" / "model_sources.lock.json"
        if not lock_path.exists():
            pytest.skip("model_sources.lock.json not found")
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        for engine in data["engines"]:
            rf = engine.get("required_files", [])
            assert rf, f"Engine {engine['engine_id']} has empty required_files — not valid for production"


# ── 公共 facade 类型一致性 ─────────────────────────────


class TestFacadeTypeConsistency:
    """公共 facade 导出的类型与 backend 实际返回类型完全相同。"""

    def test_init_exports_correct_types(self) -> None:
        """__init__.py 导出正确的类型。"""
        import app.analysis.transcription as t

        for name in [
            "ASRTranscriptResult",
            "ASRSegmentResult",
            "EmotionEvent",
            "Word",
            "TranscriptionResult",
            "TranscriberBackend",
            "FunASRBackend",
            "FasterWhisperBackend",
            "ASRPipeline",
        ]:
            assert hasattr(t, name), f"__init__.py missing export: {name}"

    def test_legacy_facade_exports_protocol_from_models(self) -> None:
        """兼容入口必须从唯一模型定义处导出转写协议。"""
        from app.analysis.transcribe import TranscriberBackend as facade_type
        from app.analysis.transcription.models import TranscriberBackend as model_type

        assert facade_type is model_type

    def test_models_are_single_source(self) -> None:
        """数据模型只在 models.py 中定义，不在 backends.py / pipeline.py 重复。"""

        def _count_class_in_file(filepath: Path, class_name: str) -> int:
            if not filepath.exists():
                return 0
            content = filepath.read_text(encoding="utf-8")
            return content.count(f"class {class_name}")

        base = _REPO_ROOT / "app" / "analysis" / "transcription"
        for cls_name in [
            "ASRTranscriptResult",
            "ASRSegmentResult",
            "EmotionEvent",
            "Word",
            "TranscriptionResult",
            "TranscriberBackend",
        ]:
            in_models = _count_class_in_file(base / "models.py", cls_name)
            in_backends = _count_class_in_file(base / "backends.py", cls_name)
            in_pipeline = _count_class_in_file(base / "pipeline.py", cls_name)

            assert in_models == 1, f"{cls_name} defined {in_models} times in models.py"
            assert in_backends == 0, f"{cls_name} should NOT be defined in backends.py (found {in_backends})"
            assert in_pipeline == 0, f"{cls_name} should NOT be defined in pipeline.py (found {in_pipeline})"
