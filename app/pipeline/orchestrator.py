"""片段处理编排。

把"转写 → 高光评分"按单个片段串联,并提供可挂到录制回调上的异步包装:

* :func:`process_segment_sync` —— 同步执行(CPU 密集:Whisper / 音频分析);
* :func:`make_pipeline_callback` —— 返回一个 ``async`` 回调,内部用线程池执行
  同步流程,避免阻塞 asyncio 录制主循环。

任何单个片段的处理失败都不会中断录制或后续片段(异常被捕获并记录)。
"""

from __future__ import annotations

import asyncio

from loguru import logger

from app.analysis.highlight import score_segment
from app.analysis.transcribe import TranscriberBackend, transcribe_segment
from app.db.models import FinalClip, HighlightCandidate, RawSegment


def process_segment_sync(
    segment_id: int,
    backend: TranscriberBackend | None = None,
) -> HighlightCandidate | None:
    """对单个片段执行完整分析流程(同步)。

    :param segment_id: ``raw_segments`` 主键。
    :param backend: 可选转写后端(便于测试注入)。
    :returns: 若产生高光候选则返回它,否则 ``None``。
    """
    try:
        transcribe_segment(segment_id, backend=backend)
    except Exception as exc:  # noqa: BLE001 — 单片段失败不应影响整体
        logger.error("片段 {} 转写失败: {}", segment_id, exc)
        return None

    try:
        return score_segment(segment_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("片段 {} 评分失败: {}", segment_id, exc)
        return None


def produce_clip(candidate_id: int, auto_upload: bool = False) -> FinalClip | None:
    """把一个高光候选生成成品切片并配上文案(阶段3)。

    :param candidate_id: ``highlight_candidates`` 主键。
    :param auto_upload: 是否在 ready 后自动入队上传(由房间 auto_upload 开关控制)。
    :returns: 成品 :class:`FinalClip`;失败时 ``None``。
    """
    # 延迟导入:切片/文案依赖较重(FFmpeg、可选 LLM),按需加载。
    from app.clipping.clipper import produce_clip as _cut_clip
    from app.publishing.copywriter import generate_copy

    try:
        clip = _cut_clip(candidate_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("候选 {} 切片失败: {}", candidate_id, exc)
        return None

    if clip.id is None:
        return clip
    try:
        clip = generate_copy(clip.id)
    except Exception as exc:  # noqa: BLE001
        logger.error("切片 {} 文案生成失败: {}", clip.id, exc)
        return clip

    # 成品就绪且上传模块开启且房间允许自动上传时,自动入队上传。
    from app.core import settings_store
    from app.db.models import ClipStatus

    if clip.status == ClipStatus.READY and settings_store.upload_active() and auto_upload and clip.id is not None:
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
    """对单个候选执行"切片 + 文案"(便于 CLI/编排复用)。

    :param candidate_id: ``highlight_candidates`` 主键。
    :returns: 成品 :class:`FinalClip` 或 ``None``。
    """
    return produce_clip(candidate_id)


def make_pipeline_callback(
    backend: TranscriberBackend | None = None,
    produce: bool = False,
    room_id: int | None = None,
):  # noqa: ANN201 — 返回异步回调
    """构造可传给 :class:`~app.recording.recorder.Recorder` 的片段回调。

    V0.1.6: 录制回调只负责创建 SegmentTask 并登记到持久化队列,
    不再同步等待转写/分析/渲染。后台 TaskWorker 异步消费队列。

    :param backend: 可选转写后端(已弃用,由 TaskWorker 统一管理)。
    :param produce: 为 ``True`` 时允许自动进入渲染阶段。
    :param room_id: 房间 id,用于读取房间级开关。
    :returns: 形如 ``async def cb(segment)`` 的协程回调。
    """
    from app.db.models import LiveRoom
    from app.db.session import get_session as _gs
    from app.pipeline.task_worker import create_task as _create_task

    # 预读房间开关配置。
    room_auto_analyze = produce
    room_auto_render = produce
    if room_id is not None:
        with _gs() as _db:
            room = _db.get(LiveRoom, room_id)
            if room is not None:
                room_auto_analyze = room.auto_analyze
                room_auto_render = room.auto_render

    async def _callback(segment: RawSegment) -> None:
        if segment.id is None or segment.session_id is None:
            return
        seg_id = segment.id
        if not room_auto_analyze:
            logger.debug("房间 auto_analyze=off,跳过片段 {} 的分析。", seg_id)
            return
        task = _create_task(seg_id, segment.session_id)
        if task is None:
            logger.debug("片段 {} 已有任务记录,幂等跳过。", seg_id)
        else:
            logger.info("流水线登记片段 segment={},任务 id={}。", seg_id, task.id)

    return _callback
