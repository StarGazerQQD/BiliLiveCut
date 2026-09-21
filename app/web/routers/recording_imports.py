"""本地录播文件流式上传与预处理作业 API。"""

from __future__ import annotations

import asyncio
import shutil
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func
from sqlmodel import select

from app.core.config import settings
from app.db.entities import RawSegment, SegmentTask, TaskStatus, Transcript
from app.db.session import get_session
from app.pipeline.task_context import event_first_context
from app.recording.import_comments import MAX_COMMENT_BYTES
from app.recording.imports import (
    MAX_VIDEO_BYTES,
    ImportRecord,
    ImportRequest,
    create_import,
    get_import,
    import_directory,
    import_lock,
    list_imports,
    save_import,
    upload_path,
)
from app.web.services.background_jobs import get_job, web_job_manager
from app.web.services.review_workflow import review_actor

router = APIRouter(prefix="/recording-imports")


def _owned(import_id: str, request: Request) -> ImportRecord:
    actor, role = review_actor(request)
    try:
        record = get_import(import_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(404, "导入记录不存在") from exc
    if role != "admin" and record.owner != actor:
        raise HTTPException(403, "无权操作此导入")
    return record


@router.get("")
def get_imports(request: Request) -> dict[str, object]:
    """返回本地来源、上传状态和预处理状态。"""
    actor, role = review_actor(request)
    records = list_imports()
    ids = [record.session_id for record in records if record.session_id is not None]
    analysis: dict[int, dict[str, int]] = {
        sid: {"total": 0, "done": 0, "failed": 0, "retrying": 0, "asr_unavailable": 0, "transcripts": 0} for sid in ids
    }
    pending = {
        TaskStatus.RECORDED,
        TaskStatus.QUEUED_FOR_TRANS,
        TaskStatus.TRANSCRIBING,
        TaskStatus.TRANSCRIBED,
        TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.ANALYZING,
        TaskStatus.STALE,
        TaskStatus.TRANSIENT_FAILED,
    }
    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.session_id.in_(ids))).all() if ids else []
        for task in tasks:
            progress = analysis[task.session_id]
            progress["total"] += 1
            if event_first_context(task.context_json).get("asr_evidence_state") in {"unavailable", "degraded"}:
                progress["asr_unavailable"] += 1
            if task.stage in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TRANSIENT_FAILED, TaskStatus.STALE}:
                if task.failed_stage in {TaskStatus.RENDERING, TaskStatus.PUBLISHING}:
                    progress["done"] += 1
                elif task.stage in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                    progress["failed"] += 1
                else:
                    progress["retrying"] += 1
            elif task.stage not in pending:
                progress["done"] += 1
        transcripts = (
            db.exec(
                select(RawSegment.session_id, func.count(Transcript.id))
                .join(Transcript, Transcript.segment_id == RawSegment.id)
                .where(RawSegment.session_id.in_(ids))
                .group_by(RawSegment.session_id)
            ).all()
            if ids
            else []
        )
        for session_id, count in transcripts:
            analysis[session_id]["transcripts"] = count
    return {
        "imports": [
            {
                **record.model_dump(mode="json"),
                "job": get_job(record.job_id) if record.job_id else None,
                "analysis": analysis.get(record.session_id),
            }
            for record in records
            if role == "admin" or record.owner == actor
        ],
        "max_video_bytes": MAX_VIDEO_BYTES,
        "max_comment_bytes": MAX_COMMENT_BYTES,
        "engine": settings.asr_primary,
    }


@router.post("", status_code=201)
def post_import(body: ImportRequest, request: Request) -> ImportRecord:
    """创建声明，随后分别上传视频及可选弹幕。"""
    actor, _ = review_actor(request)
    try:
        return create_import(body, actor)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _mark_uploaded(import_id: str, kind: Literal["video", "comments"]) -> ImportRecord:
    with get_session() as db:
        record = get_import(import_id, db)
        if kind == "video":
            record.video_uploaded = True
        else:
            record.comments_uploaded = True
        save_import(db, record)
    return record


@router.put("/{import_id}/files/{kind}")
async def put_file(import_id: str, kind: Literal["video", "comments"], request: Request) -> ImportRecord:
    """限制真实读取字节数，流式落盘并在完整上传后原子替换。"""
    record = await asyncio.to_thread(_owned, import_id, request)
    lock = import_lock(import_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "该导入正在处理，请稍后重试")
    try:
        record = await asyncio.to_thread(_owned, import_id, request)
        if record.sealed or (record.video_uploaded if kind == "video" else record.comments_uploaded):
            raise HTTPException(409, "文件已上传或导入已开始")
        try:
            destination = upload_path(record, kind)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        limit = MAX_VIDEO_BYTES if kind == "video" else MAX_COMMENT_BYTES
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError as exc:
                raise HTTPException(400, "Content-Length 无效") from exc
            if length <= 0 or length > limit:
                raise HTTPException(413, "文件为空或超过大小限制")
        partial = destination.with_suffix(destination.suffix + ".part")
        received = 0
        # 互斥期间覆盖的仅为本导入上次断电留下的暂存文件。
        stream = await asyncio.to_thread(partial.open, "wb")
        try:
            async for chunk in request.stream():
                if received + len(chunk) > limit:
                    raise HTTPException(413, "文件超过大小限制")
                for start in range(0, len(chunk), 1024 * 1024):
                    piece = chunk[start : start + 1024 * 1024]
                    if received % (64 * 1024 * 1024) < len(piece):
                        usage = await asyncio.to_thread(shutil.disk_usage, destination.parent)
                        if usage.free < len(piece) + 64 * 1024 * 1024:
                            raise HTTPException(507, "磁盘剩余空间不足")
                    await asyncio.to_thread(stream.write, piece)
                    received += len(piece)
            if not received or (declared is not None and received != int(declared)):
                raise HTTPException(400, "上传内容为空或长度不完整")
        finally:
            await asyncio.to_thread(stream.close)
        await asyncio.to_thread(partial.replace, destination)
        return await asyncio.to_thread(_mark_uploaded, import_id, kind)
    finally:
        try:
            if "partial" in locals():
                await asyncio.to_thread(partial.unlink, missing_ok=True)
        finally:
            lock.release()


@router.delete("/{import_id}")
def discard_import(import_id: str, request: Request) -> dict[str, str]:
    """清理未登记场次的上传或失败导入，不删除已进入分析流程的媒体。"""
    _owned(import_id, request)
    lock = import_lock(import_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "该导入正在处理")
    try:
        record = _owned(import_id, request)
        job = get_job(record.job_id) if record.job_id else None
        if record.session_id is not None or (job and job["status"] in {"queued", "running", "cancelling"}):
            raise HTTPException(409, "已登记场次或活动作业不能清理")
        directory = import_directory(import_id)
        if directory.exists():
            shutil.rmtree(directory)
        from app.db.entities import AppSetting

        with get_session() as db:
            for key in (f"recording_import:{import_id}", f"web_job:{record.job_id}"):
                row = db.get(AppSetting, key)
                if row:
                    db.delete(row)
        return {"status": "deleted"}
    finally:
        lock.release()


def _seal(import_id: str) -> ImportRecord:
    with get_session() as db:
        record = get_import(import_id, db)
        if not record.video_uploaded or (record.comments_name and not record.comments_uploaded):
            raise ValueError("请先完整上传视频和已选择的弹幕文件")
        record.sealed = True
        save_import(db, record)
    return record


def _save_job_id(import_id: str, job_id: str) -> None:
    with get_session() as db:
        record = get_import(import_id, db)
        record.job_id = job_id
        save_import(db, record)


@router.post("/{import_id}/start", status_code=202)
async def start_import(import_id: str, request: Request) -> dict[str, object]:
    """启动预处理；重复请求复用现有作业或已登记的场次。"""
    await asyncio.to_thread(_owned, import_id, request)
    lock = import_lock(import_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "该导入正在处理")
    try:
        record = await asyncio.to_thread(_owned, import_id, request)
        if record.job_id:
            existing = await asyncio.to_thread(get_job, record.job_id)
            if existing is not None:
                return {"job": existing, "session_id": record.session_id}
        try:
            record = await asyncio.to_thread(_seal, import_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        job = await web_job_manager.enqueue(
            "local_import",
            {"import_id": import_id},
            label=f"导入录播：{record.title}",
            owner=record.owner,
            dedup_key=f"local-import:{import_id}",
        )
        await asyncio.to_thread(_save_job_id, import_id, str(job["id"]))
        return {"job": job, "session_id": record.session_id}
    finally:
        lock.release()
