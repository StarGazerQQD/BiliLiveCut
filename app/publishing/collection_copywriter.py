"""P2 合集文案与章节生成(V0.1.6)。

基于主题整体生成:
- 主题摘要
- B站/YouTube 标题
- 视频简介
- 章节时间戳
- 各章节标题
- 封面短标题
- 标签

通过现有 LLM 完成文本生成。LLM 不可用时,使用规则回退(基于事件评分和关键词)。
"""

from __future__ import annotations

import json
import re

from loguru import logger

from app.analysis.llm import call_text, is_llm_enabled


def generate_copywriter(
    topic_title: str,
    event_summaries: list[dict],
    total_duration_s: float,
) -> dict:
    """为合集主题生成文案和章节信息。

    :param topic_title: 主题标题。
    :param event_summaries: 事件摘要列表。
    :param total_duration_s: 合集总时长(秒)。
    :returns: ``{summary, bilibili_title, youtube_title, description, chapters, tags, cover_title}``。
    """
    if is_llm_enabled():
        result = _llm_copywriter(topic_title, event_summaries, total_duration_s)
        if result:
            return result

    return _fallback_copywriter(topic_title, event_summaries, total_duration_s)


def _llm_copywriter(
    topic_title: str,
    event_summaries: list[dict],
    total_duration_s: float,
) -> dict | None:
    """通过 LLM 生成合集文案。"""
    events_text = ""
    cumulative_s = 0.0
    for i, ev in enumerate(event_summaries):
        dur = ev.get("duration_s", 30)
        start = cumulative_s
        cumulative_s += dur
        events_text += (
            f"- #{i+1} [{sec_to_hhmmss(start)} → {sec_to_hhmmss(cumulative_s)}] "
            f"评分 {ev.get('score', 0):.2f}"
        )
        reason = ev.get("reason", "")
        if reason:
            events_text += f" | 原因:{reason}"
        asr = ev.get("asr_text", "")
        if asr:
            asr_short = asr[:200].replace("\n", " ")
            events_text += f" | 内容:{asr_short}"
        events_text += "\n"

    prompt = f"""你是一个横屏长视频网站的合集写手。请基于以下主题和事件列表,生成横屏投稿所需的全部文案。

主题标题:{topic_title}
总时长:{sec_to_hhmmss(total_duration_s)} (约 {total_duration_s:.0f} 秒)

事件列表:
{events_text}

任务:
1. 主题摘要 - 用 2-3 句话总结这些事件共同讲述了什么故事,事件如何发展。
2. B站标题 - 吸引眼球、不超30字的横屏视频标题。
3. YouTube 标题 - 英文或中英双语,适合油管的标题。
4. 视频简介 - 包含主题概述、事件列表、时间戳的完整简介(100-200字)。
5. 章节时间戳 - 为每个事件起一个有吸引力的章节标题。
6. 标签 - 5-10 个关键词标签(以逗号分隔)。
7. 封面短标题 - 适合印在封面上的 5-10 字标题。

请以 JSON 格式输出:
{{"summary":"...","bilibili_title":"...","youtube_title":"...","description":"...","chapters":[{{"ts":"HH:MM:SS","title":"..."}},...],"tags":"...","cover_title":"..."}}
只输出 JSON,不要其他内容。""".strip()

    try:
        raw = call_text(prompt, max_tokens=1024)
        if raw is None:
            return None
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            return json.loads(match.group(0))
        logger.warning("LLM 合集文案返回非 JSON: {}", raw[:200])
        return None
    except Exception as exc:
        logger.warning("LLM 合集文案生成失败: {}", exc)
        return None


def _fallback_copywriter(
    topic_title: str,
    event_summaries: list[dict],
    total_duration_s: float,
) -> dict:
    """规则回退:基于事件评分和关键词生成基础文案。"""
    count = len(event_summaries)
    top_reasons = []
    for ev in event_summaries:
        reason = ev.get("reason", "")
        if reason and len(top_reasons) < 3:
            top_reasons.append(reason)

    topic_keywords = re.findall(r"[\u4e00-\u9fff\w]+", topic_title or "")
    tags = ",".join(topic_keywords[:5]) if topic_keywords else "游戏,直播"

    chapters = []
    cumulative_s = 0.0
    for i, ev in enumerate(event_summaries):
        dur = ev.get("duration_s", 30)
        chapters.append({
            "ts": sec_to_hhmmss(cumulative_s),
            "title": f"高光 #{i+1}",
        })
        cumulative_s += dur

    summary = f"「{topic_title}」合集,共{count}段高光,总时长约{sec_to_hhmmss(total_duration_s)}"
    if top_reasons:
        summary += f"。包含:{' / '.join(top_reasons)}"

    return {
        "summary": summary,
        "bilibili_title": (
            f"「{topic_title}」高光合集 · {count}段名场面一次看完"
            if topic_title else f"{count}段高光合集"
        ),
        "youtube_title": (
            f"[Highlight Montage] {topic_title} ({count} Clips)"
            if topic_title else f"Highlight Montage ({count} Clips)"
        ),
        "description": summary,
        "chapters": chapters,
        "tags": tags,
        "cover_title": topic_title if topic_title else f"高光合集 #{count}段",
    }


def sec_to_hhmmss(total_s: float) -> str:
    """秒 → HH:MM:SS。"""
    total = int(abs(total_s))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def generate_copywriter_for_topic(topic_id: int) -> dict | None:
    """从数据库加载主题和事件,批量生成文案。

    :param topic_id: 主题 id。
    :returns: 文案字典或 ``None``。
    """
    from app.db.session import get_session
    from app.db.models import HighlightCandidate, HighlightTopic, Topic
    from sqlmodel import select

    with get_session() as db:
        topic = db.get(Topic, topic_id)
        if topic is None:
            return None

        links = db.exec(
            select(HighlightTopic).where(
                HighlightTopic.topic_id == topic_id,
            ).order_by(HighlightTopic.sort_order.asc())
        ).all()

        event_summaries = []
        total_dur = 0.0
        for link in links:
            cand = db.get(HighlightCandidate, link.event_id)
            if cand is None:
                continue
            dur = (
                (cand.end_ts - cand.start_ts).total_seconds()
                if cand.start_ts and cand.end_ts else 30
            )
            total_dur += dur
            event_summaries.append({
                "candidate_id": cand.id,
                "score": cand.highlight_score,
                "reason": cand.reason or "",
                "asr_text": cand.snapshot.get("asr_text", "") if cand.snapshot else "",
                "duration_s": round(dur, 1),
            })

    return generate_copywriter(
        topic_title=topic.title or f"主题 #{topic_id}",
        event_summaries=event_summaries,
        total_duration_s=total_dur,
    )
