"""文案生成。

为成品切片生成标题 / 简介 / 标签 / 封面建议 / 发布时间建议 / 是否值得发布。

* 优先调用大模型(通过 OpenAI 兼容协议对接 DeepSeek/通义/Kimi/GLM 等);
* 未配置 LLM 时回退到**基于转写与关键词的规则文案**,保证流程不中断;
* 根据直播间审核模式(manual/semi/auto)决定成品状态,并在进入 ``ready`` 时
  把元数据导出到 ``storage/ready_to_upload``,供阶段5 的上传模块消费。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from loguru import logger
from sqlmodel import select

from app.analysis import llm as llm_mod
from app.analysis.keywords import match_keywords
from app.clipping.clipper import select_covering_segments
from app.core.config import settings
from app.core.paths import ready_to_upload_dir
from app.db.models import (
    ClipStatus,
    FinalClip,
    HighlightCandidate,
    LiveRoom,
    RecordingSession,
    RoomMode,
    Transcript,
)
from app.db.session import get_session


@dataclass(slots=True)
class Copy:
    """一条文案。

    :param title: 标题。
    :param description: 简介。
    :param tags: 标签列表。
    :param cover_suggestion: 封面建议。
    :param publish_suggestion: 发布时间/是否值得发布的综合建议文本。
    :param worth_publishing: 是否值得发布。
    """

    title: str
    description: str
    tags: list[str]
    cover_suggestion: str
    publish_suggestion: str
    worth_publishing: bool


_COPY_PROMPT = """你是一名 Bilibili 资深短视频运营。下面是一个直播切片的转写内容,\
请为它生成投稿文案。要求:标题吸引人但不浮夸失真、不做标题党;贴合 B 站社区调性;\
简介 1-3 句概括看点;标签 4-8 个;并判断是否值得发布。

切片转写:
{text}

高光理由参考:{reason}
{trend_block}
请只输出 JSON,格式:
{{"title": "标题(<=40字)", "description": "简介", "tags": ["标签1","标签2"], \
"cover_suggestion": "封面建议", "publish_suggestion": "发布时间或频道建议", \
"worth_publishing": true/false}}"""


def _trend_block() -> str:
    """构造注入到文案 prompt 的"近期网感参考"段落(未启用时为空)。

    :returns: 提示词片段;资料库未启用或为空时返回空串。
    """
    if not settings.trend_enabled:
        return ""
    try:
        from app.trends import store as trend_store

        ref = trend_store.style_reference(days=settings.trend_match_days)
    except Exception as exc:  # noqa: BLE001 — 参考缺失不应影响文案生成
        logger.warning("获取网感参考失败: {}", exc)
        return ""
    titles, tags = ref.get("titles") or [], ref.get("tags") or []
    if not titles and not tags:
        return ""
    return (
        "\n近期网感参考(可借鉴热门表达与标签风格,但不要生硬套用、不要标题党):\n"
        f"- 热门标题示例:{' / '.join(titles[:6])}\n"
        f"- 热门标签:{' '.join(tags[:12])}\n"
    )


def gather_clip_text(candidate_id: int) -> tuple[str, str]:
    """汇总候选覆盖片段的转写文本与高光理由。

    :param candidate_id: ``highlight_candidates`` 主键。
    :returns: ``(text, reason)``。
    :raises ValueError: 候选不存在时。
    """
    with get_session() as db:
        cand = db.get(HighlightCandidate, candidate_id)
        if cand is None:
            raise ValueError(f"候选不存在: id={candidate_id}")
        reason = cand.reason or ""
        session_id = cand.session_id
        start_ts, end_ts = cand.start_ts, cand.end_ts

    segments = select_covering_segments(session_id, start_ts, end_ts)
    seg_ids = [s.id for s in segments if s.id is not None]
    with get_session() as db:
        rows = db.exec(
            select(Transcript).where(Transcript.segment_id.in_(seg_ids))  # type: ignore[attr-defined]
        ).all()
    by_seg = {t.segment_id: t.text for t in rows}
    text = "".join(by_seg.get(s.id, "") for s in segments).strip()
    return text, reason


def _llm_copy(text: str, reason: str) -> Copy | None:
    """调用 LLM 生成文案;不可用或解析失败返回 ``None``。

    :param text: 切片转写文本。
    :param reason: 高光理由。
    :returns: :class:`Copy` 或 ``None``。
    """
    raw = llm_mod.call_text(
        _COPY_PROMPT.format(
            text=text or "(无转写)",
            reason=reason or "无",
            trend_block=_trend_block(),
        )
    )
    if raw is None:
        return None
    data = llm_mod.extract_json(raw)
    if not data:
        logger.warning("文案 LLM 输出无法解析: {}", raw[:200])
        return None
    tags = data.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    return Copy(
        title=str(data.get("title", "")).strip()[:80],
        description=str(data.get("description", "")).strip(),
        tags=[str(t).strip() for t in tags][:10],
        cover_suggestion=str(data.get("cover_suggestion", "")).strip(),
        publish_suggestion=str(data.get("publish_suggestion", "")).strip(),
        worth_publishing=bool(data.get("worth_publishing", True)),
    )


def _fallback_copy(text: str, reason: str) -> Copy:
    """无 LLM 时基于转写与关键词的规则文案。

    :param text: 切片转写文本。
    :param reason: 高光理由。
    :returns: :class:`Copy`。
    """
    _, hits = match_keywords(text)
    # 标题:优先用命中关键词点题,否则取转写开头。
    if hits:
        title = f"直播名场面:{' '.join(hits[:2])}"
    else:
        snippet = text[:18].strip()
        title = f"直播高光时刻:{snippet}" if snippet else "直播高光切片"
    description = (text[:120].strip() or "直播精彩片段切片。") + "\n#直播切片 #录播"
    # 无 LLM 时,借用资料库中与本片段相关的近期热门词补充标签。
    trend_tags: list[str] = []
    if settings.trend_enabled:
        try:
            from app.trends import store as trend_store

            _, trend_tags = trend_store.match_text(text, days=settings.trend_match_days)
        except Exception as exc:  # noqa: BLE001 — 参考缺失不影响回退文案
            logger.warning("回退文案获取网感标签失败: {}", exc)
    tags = list(dict.fromkeys(hits + trend_tags[:4] + ["直播切片", "录播", "高光时刻"]))[:8]
    return Copy(
        title=title[:80],
        description=description,
        tags=tags,
        cover_suggestion="选用情绪最强烈的一帧,可加大字标题。",
        publish_suggestion="建议晚间 20:00-23:00 发布。",
        worth_publishing=True,
    )


def generate_copy(clip_id: int) -> FinalClip:
    """为成品切片生成文案并按审核模式决定状态。

    :param clip_id: ``final_clips`` 主键。
    :returns: 更新后的 :class:`FinalClip`。
    :raises ValueError: 切片不存在时。
    """
    with get_session() as db:
        clip = db.get(FinalClip, clip_id)
        if clip is None:
            raise ValueError(f"切片不存在: id={clip_id}")
        candidate_id = clip.candidate_id
        cand = db.get(HighlightCandidate, candidate_id)
        session = db.get(RecordingSession, cand.session_id) if cand else None
        room = db.get(LiveRoom, session.room_id) if session else None
        mode = room.mode if room else RoomMode.MANUAL
        auto_threshold = room.auto_publish_threshold if room else 0.85
        score = cand.highlight_score if cand else 0.0

    text, reason = gather_clip_text(candidate_id)
    copy = _llm_copy(text, reason) or _fallback_copy(text, reason)

    # 状态决策:人工模式一律待审;半自动按分数;全自动按是否值得发布。
    status = _decide_status(mode, copy.worth_publishing, score, auto_threshold)

    with get_session() as db:
        clip = db.get(FinalClip, clip_id)
        clip.title = copy.title
        clip.description = copy.description
        clip.tags_json = json.dumps(copy.tags, ensure_ascii=False)
        clip.publish_suggestion = (
            f"{copy.publish_suggestion} | 封面:{copy.cover_suggestion} | "
            f"值得发布:{copy.worth_publishing}"
        )
        clip.status = status
        db.add(clip)

    logger.success(
        "文案完成 clip={} 状态={} 标题={!r}",
        clip_id,
        status,
        copy.title,
    )
    if status == ClipStatus.READY:
        export_manifest(clip_id)
    return clip


def _decide_status(
    mode: str,
    worth_publishing: bool,
    score: float,
    auto_threshold: float,
) -> str:
    """根据审核模式决定切片状态。

    :param mode: 直播间审核模式。
    :param worth_publishing: 文案判断是否值得发布。
    :param score: 候选综合分。
    :param auto_threshold: 自动发布阈值。
    :returns: :class:`~app.db.models.ClipStatus` 之一。
    """
    if mode == RoomMode.AUTO:
        return ClipStatus.READY if worth_publishing else ClipStatus.REJECTED
    if mode == RoomMode.SEMI:
        if worth_publishing and score >= auto_threshold:
            return ClipStatus.READY
        return ClipStatus.REVIEWING
    # 默认 manual:始终等待人工审核。
    return ClipStatus.REVIEWING


def export_manifest(clip_id: int) -> str:
    """把成品切片的元数据导出为 ``ready_to_upload`` 下的 JSON 清单。

    清单引用成品 MP4 与封面路径,供阶段5 的上传模块消费(不复制大文件)。

    :param clip_id: ``final_clips`` 主键。
    :returns: 清单文件路径。
    :raises ValueError: 切片不存在时。
    """
    with get_session() as db:
        clip = db.get(FinalClip, clip_id)
        if clip is None:
            raise ValueError(f"切片不存在: id={clip_id}")
        manifest = {
            "clip_id": clip.id,
            "candidate_id": clip.candidate_id,
            "file_path": clip.file_path,
            "cover_path": clip.cover_path,
            "duration_s": clip.duration_s,
            "title": clip.title,
            "description": clip.description,
            "tags": json.loads(clip.tags_json) if clip.tags_json else [],
            "publish_suggestion": clip.publish_suggestion,
            "content_hash": clip.content_hash,
            "status": clip.status,
        }
    path = ready_to_upload_dir() / f"clip_{clip_id}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已导出待上传清单: {}", path)
    return str(path)
