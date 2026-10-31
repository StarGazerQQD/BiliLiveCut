# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.clipping.core import probe_media
from app.clipping.danmaku import render_danmaku_rows
from app.clipping.drafts import _snapshot
from app.clipping.models import ClipOptions
from app.core.paths import resolve_storage_path
from app.db.entities import (
    AppSetting,
    ClipVariant,
    HighlightEvent,
    IntroTemplate,
    LiveRoom,
    RawSegment,
    RecordingSession,
    SegmentTask,
    SubtitleTemplate,
    Transcript,
)
from app.pipeline.lease import TaskLease, approved_task_candidate
from app.recording.metadata import session_title_at


class MediaFacts(BaseModel):
    """实际探测和内容摘要，不接受渲染调用方估计的媒体属性。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(gt=0)
    duration: float = Field(ge=0, allow_inf_nan=False)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


def file_stamp(path: str) -> tuple[int, int, int, int, int]:
    """用于短事务复验预检后的文件仍为同一对象。"""
    stat = resolve_storage_path(path).stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@dataclass(frozen=True)
class VerifiedMedia:
    """事务外完成媒体预检，事务内只复核文件身份。"""

    path: str
    facts: MediaFacts
    stamp: tuple[int, int, int, int, int]

    def require_unchanged(self) -> None:
        """拒绝预检后被替换或改写的文件。"""
        if file_stamp(self.path) != self.stamp:
            raise ValueError("渲染产物在媒体预检后发生改变")


def verify_media(path: str, expected: MediaFacts | None = None, *, cover: bool = False) -> VerifiedMedia:
    """读取完整摘要并探测真实视频或封面，任何缺失和不匹配均失败。"""
    stamp = file_stamp(path)
    digest = hashlib.sha256()
    with resolve_storage_path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    duration, width, height = probe_media(str(resolve_storage_path(path)))
    if not math.isfinite(duration) or width <= 0 or height <= 0:
        raise ValueError("渲染产物缺少有效的视频尺寸或媒体属性")
    if not cover and (duration < 1.0 or stamp[2] <= 1024):
        raise ValueError("渲染产物的实际时长或文件大小不足")
    facts = MediaFacts(sha256=digest.hexdigest(), size=stamp[2], duration=duration, width=width, height=height)
    proof = VerifiedMedia(path, facts, stamp)
    proof.require_unchanged()
    if expected is not None and facts != expected:
        raise ValueError("渲染产物与已确认的内容摘要或媒体属性不一致")
    return proof


def render_input_fingerprint(db: Session, task: SegmentTask, options: ClipOptions | None = None) -> str:
    """绑定实际候选、素材、字幕、弹幕、片头与编码选项，不包含处理状态。"""
    options = options or ClipOptions.from_settings()
    candidate = approved_task_candidate(db, task)
    candidate_values = _snapshot(candidate)
    candidate_values.pop("status")
    event = db.get(HighlightEvent, task.event_id)
    assert event is not None
    segments = db.exec(
        select(RawSegment)
        .where(
            RawSegment.session_id == task.session_id,
            RawSegment.start_ts < candidate.end_ts,
            RawSegment.end_ts > candidate.start_ts,
        )
        .order_by(RawSegment.seq, RawSegment.id)
    ).all()
    segment_values = []
    for segment in segments:
        values = _snapshot(segment)
        values.pop("status")
        values["file_identity"] = list(file_stamp(segment.file_path))
        segment_values.append(values)
    data: dict[str, object] = {
        "version": 1,
        "session_id": task.session_id,
        "segment_id": task.segment_id,
        "candidate": candidate_values,
        "event": _snapshot(event),
        "segments": segment_values,
        "options": asdict(options),
    }
    if options.subtitle and segments:
        data["transcripts"] = [
            _snapshot(row)
            for row in db.exec(
                select(Transcript)
                .where(Transcript.segment_id.in_([row.id for row in segments]))
                .order_by(Transcript.segment_id)
            ).all()
        ]
        subtitle_template = db.exec(
            select(SubtitleTemplate).where(
                SubtitleTemplate.is_default == True,  # noqa: E712
            )
        ).first()
        data["subtitle_template"] = _snapshot(subtitle_template) if subtitle_template is not None else None
    if options.danmaku and options.danmaku_opacity > 0:
        lag, rows = render_danmaku_rows(
            db,
            task.session_id,
            candidate.start_ts,
            (candidate.end_ts - candidate.start_ts).total_seconds(),
            options.danmaku_duration_s,
        )
        data["danmaku"] = {"lag_s": lag, "rows": [(timestamp.isoformat(), content) for timestamp, content in rows]}
    template = db.exec(select(IntroTemplate).where(IntroTemplate.is_default == True)).first()  # noqa: E712
    if template is None:
        template = db.exec(select(IntroTemplate)).first()
    if template is not None and template.intro_enabled and template.intro_text:
        session = db.get(RecordingSession, task.session_id)
        room = db.get(LiveRoom, session.room_id) if session is not None and session.room_id else None
        data["intro"] = {
            "template": _snapshot(template),
            "date": date.today().isoformat(),
            "streamer_name": room.uploader_name if room is not None else "",
            "room_title": session_title_at(db, task.session_id, candidate.start_ts),
        }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class RenderCheckpoint(BaseModel):
    """与正式变体同事务保存的渲染来源、代次及完整制品身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1, le=1)
    task_id: int
    worker_id: str
    lease_token: str
    variant_id: int
    generation: int
    candidate_id: int
    event_id: int
    input_fingerprint: str
    file_path: str
    cover_path: str | None
    media: MediaFacts
    cover: MediaFacts | None

    @property
    def key(self) -> str:
        """返回绑定变体与代次的持久键。"""
        return checkpoint_key(self.variant_id, self.generation)

    def matches(self, variant: ClipVariant) -> bool:
        """不使用不同代次或被人工更改过的产物元数据。"""
        return (
            variant.id == self.variant_id
            and variant.generation == self.generation
            and variant.event_id == self.event_id
            and variant.render_config_hash == self.input_fingerprint
            and variant.file_path == self.file_path
            and variant.cover_path == self.cover_path
            and variant.file_hash == self.media.sha256
            and variant.duration_s == self.media.duration
        )

    def verify(self) -> tuple[VerifiedMedia, VerifiedMedia | None]:
        """在数据库写事务之外校验已就位的完整产物。"""
        media = verify_media(self.file_path, self.media)
        if (self.cover_path is None) != (self.cover is None):
            raise ValueError("渲染断点的封面身份不完整")
        cover = verify_media(self.cover_path, self.cover, cover=True) if self.cover_path else None
        return media, cover


def checkpoint_key(variant_id: int, generation: int) -> str:
    """返回当前恢复协议的持久键。"""
    return f"render_checkpoint:v1:{variant_id}:{generation}"


def read_checkpoint(db: Session, variant: ClipVariant) -> RenderCheckpoint | None:
    """只读同代次断点；历史产物缺少证明时不推断成功。"""
    row = db.get(AppSetting, checkpoint_key(variant.id, variant.generation))
    if row is None:
        return None
    record = RenderCheckpoint.model_validate_json(row.value)
    return record if record.matches(variant) else None


def save_checkpoint(
    db: Session,
    lease: TaskLease,
    variant: ClipVariant,
    candidate_id: int,
    media: VerifiedMedia,
    cover: VerifiedMedia | None,
) -> RenderCheckpoint:
    """在已锁定并验证租约的变体事务内保存原始产物身份。"""
    record = RenderCheckpoint(
        task_id=lease.task_id,
        worker_id=lease.worker_id,
        lease_token=lease.lease_token,
        variant_id=variant.id,
        generation=variant.generation,
        candidate_id=candidate_id,
        event_id=variant.event_id,
        input_fingerprint=variant.render_config_hash,
        file_path=variant.file_path,
        cover_path=variant.cover_path,
        media=media.facts,
        cover=cover.facts if cover else None,
    )
    db.add(AppSetting(key=record.key, value=record.model_dump_json(), updated_at=datetime.now(UTC)))
    return record
