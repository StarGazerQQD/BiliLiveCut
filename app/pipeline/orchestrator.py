"""片段处理编排。(V0.1.11-alpha 状态机重构)

V0.1.11-alpha:
- auto_* 五个开关逐阶段独立生效,不再使用旧 mode 字段
- config 在阶段转换时重新读取,不做一次性快照
- 状态转换矩阵集中定义在 task_worker.py 中
"""

from __future__ import annotations

from loguru import logger

from app.analysis.highlight import score_segment
from app.analysis.transcribe import TranscriberBackend, transcribe_segment
from app.db.models import FinalClip, HighlightCandidate, RawSegment


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
            "auto_approve_threshold": 0.82,
            "review_threshold": 0.50,
        }
    from app.db.models import LiveRoom
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
                "auto_approve_threshold": 0.82,
                "review_threshold": 0.50,
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


def produce_clip(candidate_id: int, auto_upload: bool = False) -> FinalClip | None:
    """把一个高光候选生成成品切片并配上文案。

    :param candidate_id: highlight_candidates 主键。
    :param auto_upload: 是否在 ready 后自动入队上传。
    """
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
        task = _create_task(seg_id, segment.session_id)
        if task is None:
            logger.debug("片段 {} 已有任务记录,幂等跳过。", seg_id)
        else:
            logger.info("登记片段 segment={},任务 id={}。", seg_id, task.id)

    return _callback
