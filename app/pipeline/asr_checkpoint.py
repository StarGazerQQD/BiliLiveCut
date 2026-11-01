# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
import os
import wave
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from app.analysis.transcription.backends import FasterWhisperBackend, FunASRBackend
from app.analysis.transcription.models import ASRTranscriptResult, transcript_from_payload
from app.analysis.transcription.remote import RemoteChunk
from app.analysis.transcription.speech_evidence import require_final_result
from app.db.entities import AppSetting, SegmentTask
from app.db.session import get_session
from app.pipeline.lease import TaskLease, require_current_lease


class CheckpointValue(BaseModel):
    """不含模型凭据的版本化断点，完整归属由外部键和租约共同确定。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    version: int = Field(default=1, ge=1, le=1)
    payload: dict[str, object]


def _installed_models() -> dict[str, object] | None:
    models_dir = os.environ.get("BLC_MODELS_DIR")
    if not models_dir:
        return None
    path = Path(models_dir) / "engine-pack-installed.json"
    if not path.is_file():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("engines"), dict):
        raise ValueError("已安装模型清单缺少引擎身份，不能恢复 ASR 断点")
    engines: dict[str, object] = {}
    for engine_id, record in manifest["engines"].items():
        if not isinstance(record, dict):
            raise ValueError("已安装模型清单的引擎记录无效")
        engines[engine_id] = {key: record.get(key) for key in ("identity", "content_fingerprint", "files")}
    return {"model_set_fingerprint": manifest.get("model_set_fingerprint"), "engines": engines}


class ASRCheckpoint:
    """父进程在真实租约下保存原始转写和远端已确认分块。"""

    def __init__(
        self,
        lease: TaskLease,
        audio_path: str,
        settings_payload: dict[str, object],
        prompt: str | None,
        window: tuple[float, float] | None,
        check_cancelled: Callable[[], None],
    ) -> None:
        self.lease = lease
        audio_hash = hashlib.sha256()
        with wave.open(audio_path, "rb") as audio:
            audio_hash.update(str(audio.getparams()).encode())
            while block := audio.readframes(65536):
                check_cancelled()
                audio_hash.update(block)
        backend = FunASRBackend()
        identity = {
            "pcm": audio_hash.hexdigest(),
            "prompt": prompt,
            "window": window,
            "settings": settings_payload,
            "models_dir": os.environ.get("BLC_MODELS_DIR", ""),
            "installed_models": _installed_models(),
            "models": [
                backend.primary_identity,
                backend.nano_identity,
                backend.sensevoice_identity,
                FasterWhisperBackend().model_identity,
            ],
        }
        with get_session() as db:
            require_current_lease(db, lease)
            task = db.get(SegmentTask, lease.task_id)
            assert task is not None
            identity.update(session_id=task.session_id, segment_id=task.segment_id)
        # 只持久化摘要，解析后的连接配置及密钥不写入断点或日志。
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
        self.prefix = f"asr_checkpoint:v1:{lease.task_id}:{digest}:"

    def raw(self) -> ASRTranscriptResult | None:
        """读取当前输入和配置对应的完整原始结果，再走现有终态校验。"""
        with get_session() as db:
            require_current_lease(db, self.lease)
            row = db.get(AppSetting, self.prefix + "raw")
            payload = CheckpointValue.model_validate_json(row.value).payload if row is not None else None
        return require_final_result(transcript_from_payload(payload)) if payload is not None else None

    def chunks(self) -> list[dict[str, object]]:
        """仅返回相同输入身份下已经提交成功的块。"""
        with get_session() as db:
            require_current_lease(db, self.lease)
            rows = db.exec(select(AppSetting).where(AppSetting.key.startswith(self.prefix + "chunk:"))).all()
            return [
                RemoteChunk.model_validate(CheckpointValue.model_validate_json(row.value).payload).model_dump()
                for row in rows
            ]

    def save_raw(self, result: ASRTranscriptResult) -> None:
        """原始结果提交后才进入 aliases、质量评估和文本后处理。"""
        self._save("raw", asdict(require_final_result(result)))

    def save_chunk(self, payload: dict[str, object]) -> None:
        """确认块结构后，以当前完整租约提交；失败时不能向子进程确认。"""
        chunk = RemoteChunk.model_validate(payload)
        self._save(f"chunk:{chunk.index}", chunk.model_dump())

    def _save(self, suffix: str, payload: dict[str, object]) -> None:
        encoded = CheckpointValue(payload=payload).model_dump_json()
        with get_session() as db:
            require_current_lease(db, self.lease)
            key = self.prefix + suffix
            row = db.get(AppSetting, key)
            if row is None:
                row = AppSetting(key=key)
            row.value = encoded
            row.updated_at = datetime.now(UTC)
            db.add(row)
