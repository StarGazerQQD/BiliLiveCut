"""片段处理编排。状态机重构)

V0.1.11-alpha:
- auto_* 五个开关逐阶段独立生效,不再使用旧 mode 字段
- config 在阶段转换时重新读取,不做一次性快照
- 状态转换矩阵集中定义在 task_worker.py 中
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import JsonValue

from app.analysis.highlight import score_segment
from app.analysis.transcription.models import TranscriberBackend
from app.analysis.transcription.pipeline import transcribe_segment
from app.core.config import settings
from app.core.execution import run_sync
from app.core.runtime_settings import configured_task
from app.db.entities import FinalClip, HighlightCandidate, RawSegment

if TYPE_CHECKING:
    from app.jobs.context import JobContext
    from app.pipeline.lease import TaskLease


@configured_task
def process_segment_sync(
    segment_id: int,
    backend: TranscriberBackend | None = None,
) -> HighlightCandidate | None:
    """对单个片段执行完整分析流程(同步)。

    :returns: 若产生高光候选则返回它,否则 ``None``。
    """
    try:
        transcribe_segment(segment_id, backend=backend)
    except Exception as exc:  # noqa: BLE001
        logger.error("片段 {} 转写失败: {}", segment_id, exc)
        return None
    try:
        return score_segment(segment_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("片段 {} 评分失败: {}", segment_id, exc)
        return None


def _read_room_config(room_id: int | None) -> dict:
    """读取房间级开关配置 (V0.1.11-alpha: 每次阶段转换时重新读取)。"""
    if room_id is None:
        return {
            "auto_record": False,
            "auto_analyze": False,
            "auto_render": False,
            "auto_approve": False,
            "auto_upload": False,
            "auto_approve_threshold": settings.highlight_auto_approve_threshold,
            "review_threshold": settings.highlight_review_threshold,
        }
    from app.db.entities import LiveRoom
    from app.db.session import get_session as _gs

    with _gs() as _db:
        room = _db.get(LiveRoom, room_id)
        if room is None:
            return {
                "auto_record": False,
                "auto_analyze": False,
                "auto_render": False,
                "auto_approve": False,
                "auto_upload": False,
                "auto_approve_threshold": settings.highlight_auto_approve_threshold,
                "review_threshold": settings.highlight_review_threshold,
            }
        return {
            "auto_record": room.auto_record,
            "auto_analyze": room.auto_analyze,
            "auto_render": room.auto_render,
            "auto_approve": room.auto_approve,
            "auto_upload": room.auto_upload,
            "auto_approve_threshold": room.auto_approve_threshold,
            "review_threshold": room.review_threshold,
        }


@configured_task
def produce_clip(
    candidate_id: int,
    auto_upload: bool = False,
    *,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
    output_suffix: str | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    render_variants: bool = True,
    task_lease: TaskLease | None = None,
    job_context: JobContext | None = None,
    result_metadata: dict[str, JsonValue] | None = None,
) -> FinalClip | None:
    """把一个高光候选生成成品切片并配上文案。

    :param candidate_id: highlight_candidates 主键。
    :param auto_upload: 是否在 ready 后自动入队上传。
    :param start_ts: 可选的显式剪辑起点。
    :param end_ts: 可选的显式剪辑终点。
    :param output_suffix: 可选的渲染版本标识。
    :param progress_callback: 可选的进度回调。
    :param cancel_check: 可选的取消检查。
    :param render_variants: 是否生成派生版本。
    :param task_lease: 可选的人工任务租约，成片和文案提交前复验。
    """
    from app.clipping.core import prepare_clip
    from app.clipping.drafts import publication, publish_draft
    from app.core.process_control import ProcessCancelledError
    from app.db.entities import ClipStatus, SegmentTask, TaskStatus
    from app.db.session import get_session
    from app.pipeline.lease import LeaseLostError, still_owns_lease
    from app.pipeline.stage_result import enqueue_next, mark_completed
    from app.publishing.copywriter import export_manifest, prepare_copy

    draft = prepare_clip(
        candidate_id,
        start_ts=start_ts,
        end_ts=end_ts,
        output_suffix=output_suffix,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        render_variants=render_variants,
        task_lease=task_lease,
    )
    with publication(draft.artifacts):
        if progress_callback is not None:
            progress_callback(92, "正在生成文案")
        prepare_copy(draft.clip)
        transaction = job_context.result_transaction() if job_context is not None else get_session()
        with transaction as db:
            if job_context is None:
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if task_lease is not None and not still_owns_lease(db, task_lease):
                raise LeaseLostError(f"结果提交前租约已失效: task={task_lease.task_id}")
            if cancel_check is not None and cancel_check():
                raise ProcessCancelledError("用户取消了出片")
            clip = publish_draft(db, draft)
            if task_lease is not None:
                task = db.get(SegmentTask, task_lease.task_id)
                assert task is not None
                mark_completed(task)
                enqueue_next(task, TaskStatus.RENDERED, clip_id=clip.id)
                db.add(task)
            if job_context is not None:
                job_context.complete(
                    db,
                    {
                        **(result_metadata or {}),
                        "candidate_id": candidate_id,
                        "clip_id": clip.id,
                        "file_path": clip.file_path,
                    },
                )
    from app.notify.webhook import notify_clip_complete

    notify_clip_complete(candidate_id, clip.file_path, clip.duration_s)
    if clip.status == ClipStatus.READY and clip.id is not None:
        export_manifest(clip.id)

    # V0.1.11-alpha: auto_upload 明确检查。
    if clip.status == "ready" and auto_upload and clip.id is not None:
        try:
            from app.publishing.uploader import enqueue_and_upload

            enqueue_and_upload(clip.id)
            try:
                from app.notify.webhook import notify_upload_complete

                notify_upload_complete(clip.id, clip.title)
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            logger.error("切片 {} 自动上传失败: {}", clip.id, exc)
    return clip


def process_candidate(candidate_id: int) -> FinalClip | None:
    """处理单个 Candidate,生成最终剪辑 (编排器入口)。"""
    return produce_clip(candidate_id)


def make_pipeline_callback(
    backend: TranscriberBackend | None = None,
    produce: bool = False,
    room_id: int | None = None,
):  # noqa: ANN201
    """构造可传给 Recorder 的片段回调。

    V0.1.11-alpha: 回调只负责创建 SegmentTask(录制完成时的登记),
    后续所有阶段由 TaskWorker 异步处理。每次阶段转换都重新读取房间配置。

    :param produce: 为 True 时允许自动进入渲染阶段。
    :param room_id: 房间 id,用于读取房间级开关。
    """
    from app.pipeline.task_worker import create_task as _create_task

    async def _callback(segment: RawSegment) -> None:
        if segment.id is None or segment.session_id is None:
            return
        # V0.1.11-alpha: 每次回调都重新读取配置。
        cfg = _read_room_config(room_id)
        seg_id = segment.id
        # Phase 3: auto_analyze=false 时只登记片段不创建任务。
        if not cfg["auto_analyze"]:
            logger.debug("auto_analyze=off,跳过片段 {} 的分析登记。", seg_id)
            return
        task = await run_sync(partial(_create_task, seg_id, segment.session_id))
        if task is None:
            logger.debug("片段 {} 已有任务记录,幂等跳过。", seg_id)
        else:
            logger.info("登记片段 segment={},任务 id={}。", seg_id, task.id)

    return _callback
