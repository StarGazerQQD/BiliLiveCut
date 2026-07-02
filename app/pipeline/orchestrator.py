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


def produce_clip(candidate_id: int) -> FinalClip | None:
    """把一个高光候选生成成品切片并配上文案(阶段3)。

    :param candidate_id: ``highlight_candidates`` 主键。
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

    # 成品就绪且上传模块开启时,自动入队并上传(全自动链路)。
    from app.core import settings_store
    from app.db.models import ClipStatus

    if clip.status == ClipStatus.READY and settings_store.upload_active() and clip.id is not None:
        try:
            from app.publishing.uploader import enqueue_and_upload

            enqueue_and_upload(clip.id)
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
):  # noqa: ANN201 — 返回异步回调
    """构造可传给 :class:`~app.recording.recorder.Recorder` 的片段回调。

    回调在独立线程中运行 CPU 密集流程,避免阻塞录制事件循环。

    :param backend: 可选转写后端。
    :param produce: 为 ``True`` 时,产生候选后立即自动切片+文案(全自动链路)。
    :returns: 形如 ``async def cb(segment)`` 的协程回调。
    """

    async def _callback(segment: RawSegment) -> None:
        if segment.id is None:
            return
        seg_id = segment.id
        logger.info("流水线接收片段 segment={},提交后台分析。", seg_id)
        candidate = await asyncio.to_thread(process_segment_sync, seg_id, backend)
        if produce and candidate is not None and candidate.id is not None:
            cand_id = candidate.id
            logger.info("候选 {} 已生成,自动进入切片+文案。", cand_id)
            await asyncio.to_thread(produce_clip, cand_id)

    return _callback
