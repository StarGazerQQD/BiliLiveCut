"""本地录播暂存、媒体预处理及原子发布分析任务。"""

from __future__ import annotations

import csv
import json
import math
import shutil
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.core.config import settings
from app.core.paths import raw_dir
from app.core.process_control import run_cancellable
from app.db.entities import AppSetting, Danmaku, LiveRoom, RawSegment, RecordingSession, SegmentTask, SessionStatus
from app.db.session import get_session
from app.pipeline.stage_result import make_pipeline_key, make_stage_key
from app.recording.import_comments import COMMENT_SUFFIXES, CommentReport, preprocess_comments
from app.web.services.background_jobs import JobContext

MAX_VIDEO_BYTES = 50 * 1024**3
VIDEO_SUFFIXES = {".mp4", ".mkv", ".flv", ".ts", ".mov", ".webm", ".m4v"}
_INPUT_FORMATS = "mov,matroska,webm,flv,mpegts"
_PREFIX = "recording_import:"
_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()


class ImportRequest(BaseModel):
    """创建导入时的文件声明；服务端从不接受客户端本地路径。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    title: str = Field(min_length=1, max_length=200)
    video_name: str = Field(min_length=1, max_length=255)
    comments_name: str | None = Field(default=None, min_length=1, max_length=255)
    offset_s: float = Field(default=0, ge=-604800, le=604800)
    request_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class ImportRecord(ImportRequest):
    """版本化导入清单；session_id 与全部分析输入同事务提交。"""

    version: Literal[1] = 1
    id: str
    owner: str
    created_at: datetime
    video_uploaded: bool = False
    comments_uploaded: bool = False
    sealed: bool = False
    job_id: str | None = None
    session_id: int | None = None
    segment_count: int | None = None
    comment_count: int | None = None
    duration_s: float | None = None
    warnings: list[str] = Field(default_factory=list)


def import_lock(import_id: str) -> threading.Lock:
    """同一导入的上传、启动、预处理互斥；其他导入可独立执行。"""
    with _LOCK_GUARD:
        return _LOCKS.setdefault(import_id, threading.Lock())


def import_directory(import_id: str) -> Path:
    """只允许服务端 UUID，且拒绝通过符号链接逃逸媒体根目录。"""
    if len(import_id) != 32 or any(char not in "0123456789abcdef" for char in import_id):
        raise ValueError("导入编号无效")
    root = raw_dir().resolve()
    directory = (root / "imports" / import_id).resolve()
    if not directory.is_relative_to(root):
        raise ValueError("导入目录超出媒体根目录")
    return directory


def save_import(db: Session, record: ImportRecord) -> None:
    """在调用方事务内保存清单。"""
    key = _PREFIX + record.id
    row = db.get(AppSetting, key) or AppSetting(key=key)
    row.value = record.model_dump_json()
    row.updated_at = datetime.now(UTC)
    db.add(row)


def get_import(import_id: str, db: Session | None = None) -> ImportRecord:
    """读取严格格式的导入记录，不对缺失或损坏数据伪造成功。"""
    import_directory(import_id)
    if db is None:
        with get_session() as connection:
            return get_import(import_id, connection)
    row = db.get(AppSetting, _PREFIX + import_id)
    if row is None:
        raise LookupError("导入记录不存在")
    return ImportRecord.model_validate_json(row.value)


def list_imports() -> list[ImportRecord]:
    """按创建时间倒序列出独立录播来源。"""
    with get_session() as db:
        rows = db.exec(select(AppSetting).where(AppSetting.key.startswith(_PREFIX))).all()
        records = [ImportRecord.model_validate_json(row.value) for row in rows]
    return sorted(records, key=lambda item: item.created_at, reverse=True)


def create_import(request: ImportRequest, owner: str) -> ImportRecord:
    """校验文件声明并创建可恢复的上传清单。"""
    for name, suffixes in ((request.video_name, VIDEO_SUFFIXES), (request.comments_name, COMMENT_SUFFIXES)):
        if name is None:
            continue
        if "/" in name or "\\" in name or "\x00" in name or Path(name).suffix.lower() not in suffixes:
            raise ValueError("文件名或格式不支持")
    if not request.title.strip():
        raise ValueError("标题不能为空白")
    import_id = request.request_id or uuid4().hex
    with import_lock(import_id), get_session() as db:
        try:
            existing = get_import(import_id, db)
        except LookupError:
            existing = None
        if existing is not None:
            original = ImportRequest.model_validate(existing.model_dump(include=set(ImportRequest.model_fields)))
            if existing.owner != owner or original != request:
                raise ValueError("导入请求编号已使用，请新建导入")
            return existing
        record = ImportRecord(**request.model_dump(), id=import_id, owner=owner, created_at=datetime.now(UTC))
        directory = import_directory(record.id)
        directory.mkdir(parents=True, exist_ok=True)
        save_import(db, record)
        return record


def upload_path(record: ImportRecord, kind: Literal["video", "comments"]) -> Path:
    """生成与用户文件名无关的受控目标路径。"""
    name = record.video_name if kind == "video" else record.comments_name
    if name is None:
        raise ValueError("本次导入未声明弹幕文件")
    return import_directory(record.id) / (kind + Path(name).suffix.lower())


def _probe_video(path: Path, context: JobContext) -> float:
    result = run_cancellable(
        [
            settings.ffprobe_path,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            _INPUT_FORMATS,
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=True,
        cancel_check=context.cancelled,
    )
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
        types = {stream["codec_type"] for stream in data["streams"]}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("无法读取录播时长或媒体流") from exc
    if not math.isfinite(duration) or duration <= 0 or duration > 604800:
        raise ValueError("视频时长必须大于 0 且不超过 7 天")
    if not {"video", "audio"} <= types:
        raise ValueError("录播必须同时包含视频流和音频流，才能进行本地语音分析")
    return duration


def _split_video(path: Path, directory: Path, context: JobContext) -> list[tuple[Path, float, float]]:
    directory.mkdir(exist_ok=True)
    context.report(15, "正在按关键帧分段，长视频可能需要数分钟")
    run_cancellable(
        [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            _INPUT_FORMATS,
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "disabled",
            "-f",
            "segment",
            "-segment_time",
            str(settings.segment_duration_s),
            "-reset_timestamps",
            "1",
            "-segment_list",
            str(directory / "segments.csv"),
            "-segment_list_type",
            "csv",
            str(directory / "part_%06d.mkv"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=6 * 3600,
        check=True,
        cancel_check=context.cancelled,
    )
    segments: list[tuple[Path, float, float]] = []
    with (directory / "segments.csv").open(encoding="utf-8", newline="") as stream:
        for index, row in enumerate(csv.reader(stream)):
            context.check_cancelled()
            expected = directory / f"part_{index:06d}.mkv"
            if len(row) != 3 or Path(row[0]).name != expected.name or not expected.is_file():
                raise ValueError("媒体分段清单无效或文件缺失")
            start, end = float(row[1]), float(row[2])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise ValueError("媒体分段时间无效")
            if segments and abs(start - segments[-1][2]) > 0.5:
                raise ValueError("媒体分段时间不连续")
            segments.append((expected, start, end))
    if not segments:
        raise ValueError("没有生成可分析的媒体分段")
    if segments[0][1] > 0.5:
        raise ValueError("媒体分段起点未对齐视频零点")
    return segments


def prepare_import(context: JobContext, import_id: str) -> dict[str, object]:
    """可恢复地预处理文件；所有媒体就绪后一次性发布场次、弹幕和任务。"""
    with import_lock(import_id):
        record = get_import(import_id)
        if record.session_id is not None:
            upload_path(record, "video").unlink(missing_ok=True)
            return {"import_id": import_id, "session_id": record.session_id}
        if not record.sealed or not record.video_uploaded or (record.comments_name and not record.comments_uploaded):
            raise ValueError("文件尚未上传完整")
        context.check_cancelled()
        context.report(5, "正在检查视频媒体流与时长")
        video = upload_path(record, "video")
        duration = _probe_video(video, context)
        report = (
            preprocess_comments(upload_path(record, "comments"), duration, offset_s=record.offset_s)
            if record.comments_name
            else CommentReport([], 0, 0)
        )
        context.report(10, f"弹幕预处理完成：{len(report.events)} 条有效事件")
        directory = video.parent / "segments"
        _remove_unpublished_parts(directory)
        published = False
        try:
            if shutil.disk_usage(video.parent).free < video.stat().st_size + 256 * 1024**2:
                raise ValueError("磁盘空间不足，需要保留至少一份视频大小加 256 MiB 用于分段")
            parts = _split_video(video, directory, context)
            if abs(parts[-1][2] - duration) > max(2.0, duration * 0.001):
                raise ValueError("分段总时长与原视频不一致，可能存在损坏或不连续时间戳")
            context.report(85, "正在登记分析输入与持久任务")
            context.check_cancelled()
            with get_session() as db:
                record = get_import(import_id, db)
                _publish_import(db, record, parts, report, duration, context)
            published = True
        finally:
            if not published:
                _remove_unpublished_parts(directory)
        video.unlink(missing_ok=True)
        return {"import_id": import_id, "session_id": record.session_id}


def _remove_unpublished_parts(directory: Path) -> None:
    """只清理已验证导入目录中尚未发布的本次分段文件。"""
    if directory.resolve() != directory or not directory.is_relative_to(raw_dir().resolve()):
        raise ValueError("分段目录无效")
    if directory.exists():
        for path in directory.iterdir():
            if path.name == "segments.csv" or (path.name.startswith("part_") and path.suffix == ".mkv"):
                path.unlink()


def _publish_import(
    db: Session,
    record: ImportRecord,
    parts: list[tuple[Path, float, float]],
    report: CommentReport,
    duration: float,
    context: JobContext,
) -> None:
    origin = record.created_at
    room = LiveRoom(
        platform="local",
        input_url=f"import:{record.id}",
        title=record.title,
        authorized=True,
        auto_analyze=True,
        highlight_threshold=settings.highlight_threshold,
        review_threshold=settings.highlight_review_threshold,
    )
    db.add(room)
    db.flush()
    assert room.id is not None
    session = RecordingSession(
        room_id=room.id, status=SessionStatus.STOPPED, started_at=origin, ended_at=origin + timedelta(seconds=duration)
    )
    db.add(session)
    db.flush()
    assert session.id is not None
    for index, event in enumerate(report.events):
        if index % 1000 == 0:
            context.check_cancelled()
            db.flush()
        # 本地来源没有平台房号，使用所属本地来源主键（平台标签明确为 local）。
        db.add(
            Danmaku(
                session_id=session.id,
                room_id=room.id,
                ts=origin + timedelta(seconds=event.offset_s),
                content=event.text,
                user=event.user,
            )
        )
    for seq, (path, start, end) in enumerate(parts):
        context.check_cancelled()
        segment = RawSegment(
            session_id=session.id,
            seq=seq,
            file_path=str(path),
            start_ts=origin + timedelta(seconds=start),
            end_ts=origin + timedelta(seconds=end),
            duration_s=end - start,
            size_bytes=path.stat().st_size,
        )
        db.add(segment)
        db.flush()
        assert segment.id is not None
        db.add(
            SegmentTask(
                segment_id=segment.id,
                session_id=session.id,
                pipeline_key=make_pipeline_key(segment.id),
                stage_key=make_stage_key(segment.id, "recorded"),
            )
        )
    record.session_id = session.id
    record.segment_count = len(parts)
    record.comment_count = len(report.events)
    record.duration_s = duration
    record.warnings = []
    if report.outside_count:
        record.warnings.append(f"已忽略 {report.outside_count} 条视频时间范围外的事件")
    if report.empty_count:
        record.warnings.append(f"已忽略 {report.empty_count} 条空文本或绘图事件")
    save_import(db, record)
    db.add(
        AppSetting(
            key=f"local_source:{session.id}",
            value=json.dumps(
                {
                    "version": 1,
                    "import_id": record.id,
                    "has_comments": bool(record.comments_name),
                }
            ),
        )
    )
    context.check_cancelled()
