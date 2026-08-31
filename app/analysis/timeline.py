"""录制场次高光时间轴的纯函数与弹幕解释工具。"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import select

from app.core.config import settings
from app.db.entities import Danmaku, DanmakuType
from app.db.session import get_session

TIMELINE_ANALYSIS_VERSION = 1

_REACTION_WORDS = {
    "?",
    "??",
    "???",
    "!",
    "!!",
    "!!!",
    "666",
    "6666",
    "草",
    "绷",
    "笑死",
    "哈哈",
    "哈哈哈",
    "卧槽",
    "绝了",
    "高能",
    "泪目",
    "牛逼",
}
_HUMOR_WORDS = ("哈哈", "笑", "绷", "草", "乐", "典", "节目效果", "梗")
_REACTION_ONLY_PATTERN = re.compile(r"^[?？!！6wW哈啊哦草艹绷]+$")


@dataclass(slots=True)
class _DanmakuStat:
    """一条规范化弹幕的稳定统计。"""

    key: str
    text: str
    count: int
    first_index: int


def align_danmaku_window(
    start_ts: datetime,
    end_ts: datetime,
    *,
    lag_s: float | None = None,
) -> tuple[datetime, datetime]:
    """把画面时间窗换算为弹幕接收时间窗。

    B 站客户端收到弹幕通常比画面中的真实爆点晚数秒。数据库保留真实接收
    时间，本函数只在评分与展示查询时向后平移窗口，不篡改原始数据。
    """
    delay = settings.danmaku_event_lag_s if lag_s is None else max(0.0, lag_s)
    offset = timedelta(seconds=delay)
    return start_ts + offset, end_ts + offset


def representative_danmaku(
    session_id: int,
    start_ts: datetime,
    end_ts: datetime,
    *,
    limit: int = 2,
    lag_s: float | None = None,
    include_role: bool = False,
) -> list[dict[str, object]]:
    """返回兼顾高频反应与信息量的确定性代表弹幕。"""
    if limit <= 0 or end_ts <= start_ts:
        return []
    receive_start, receive_end = align_danmaku_window(start_ts, end_ts, lag_s=lag_s)
    with get_session() as db:
        rows = db.exec(
            select(Danmaku)
            .where(
                Danmaku.session_id == session_id,
                Danmaku.msg_type == DanmakuType.DANMAKU,
                Danmaku.ts >= receive_start,
                Danmaku.ts <= receive_end,
            )
            .order_by(Danmaku.ts.asc())
        ).all()

    return select_representative_danmaku(
        [row.content for row in rows],
        limit=limit,
        include_role=include_role,
    )


def select_representative_danmaku(
    messages: Sequence[str | None],
    *,
    limit: int = 2,
    include_role: bool = False,
) -> list[dict[str, object]]:
    """从弹幕正文中稳定选择 reaction、information 与可选 humorous 样本。

    排序只依赖规范化正文、出现次数和首次出现位置，因此同一输入始终得到
    相同输出。``include_role`` 供 HotspotEvent 保存选择理由；旧时间线接口
    默认仍只返回 ``text`` 与 ``count``。
    """
    if limit <= 0:
        return []
    stats_by_key: dict[str, _DanmakuStat] = {}
    for index, content in enumerate(messages):
        text = _clean_danmaku(content)
        if not text:
            continue
        key = text.casefold()
        existing = stats_by_key.get(key)
        if existing is None:
            stats_by_key[key] = _DanmakuStat(key=key, text=text, count=1, first_index=index)
        else:
            existing.count += 1
    stats = list(stats_by_key.values())
    if not stats:
        return []

    chosen: list[tuple[_DanmakuStat, str]] = []
    used: set[str] = set()

    def choose(
        candidates: Sequence[_DanmakuStat],
        role: str,
        key: Callable[[_DanmakuStat], tuple[object, ...]],
    ) -> None:
        if len(chosen) >= limit:
            return
        available = [item for item in candidates if item.key not in used]
        if not available:
            return
        selected = min(available, key=key)
        chosen.append((selected, role))
        used.add(selected.key)

    reactions = [item for item in stats if _reaction_score(item.text) > 0.0]
    choose(
        reactions,
        "reaction",
        lambda item: (-item.count, -_reaction_score(item.text), item.first_index, item.key),
    )
    informative = [item for item in stats if _information_score(item.text) > 0.0]
    choose(
        informative,
        "information",
        lambda item: (-_information_score(item.text), -item.count, item.first_index, item.key),
    )
    humorous = [item for item in stats if _humor_score(item.text) > 0.0]
    choose(
        humorous,
        "humorous",
        lambda item: (
            -_humor_score(item.text),
            -_information_score(item.text),
            -item.count,
            item.first_index,
            item.key,
        ),
    )
    for item in sorted(
        (item for item in stats if item.key not in used),
        key=lambda item: (-item.count, -_information_score(item.text), item.first_index, item.key),
    ):
        if len(chosen) >= limit:
            break
        role = "reaction" if _reaction_score(item.text) > 0.0 else "information"
        chosen.append((item, role))
        used.add(item.key)

    result: list[dict[str, object]] = []
    for item, role in chosen:
        payload: dict[str, object] = {"text": item.text, "count": item.count}
        if include_role:
            payload["role"] = role
        result.append(payload)
    return result


def source_signals(features: dict[str, float], *, keyword_hits: list[str] | None = None) -> list[str]:
    """把模型维度转换为用户可读的高光来源标签。"""
    labels: list[str] = []
    thresholds = (
        ("danmaku", "弹幕高峰", 0.35),
        ("danmaku_sentiment", "弹幕情绪", 0.35),
        ("volume", "音量突增", 0.35),
        ("speech_rate", "语速突增", 0.35),
        ("laughter", "笑声/惊呼", 0.25),
        ("audio_events", "音频事件", 0.25),
        ("keywords", "关键词", 0.2),
        ("trend", "热点关联", 0.35),
    )
    for key, label, threshold in thresholds:
        if float(features.get(key, 0.0)) >= threshold:
            labels.append(label)
    if keyword_hits and "关键词" not in labels:
        labels.append("关键词")
    return labels or ["综合判断"]


def confidence_score(rule_score: float, llm_score: float | None, signals: list[str]) -> float:
    """计算用于时间轴展示的可解释置信度。"""
    rule = _clamp(rule_score)
    if llm_score is None:
        base = rule
    else:
        llm = _clamp(llm_score)
        agreement = 1.0 - abs(rule - llm)
        base = rule * 0.35 + llm * 0.45 + agreement * 0.20
    diversity_bonus = min(max(len(signals) - 1, 0) * 0.03, 0.09)
    return round(_clamp(base + diversity_bonus), 3)


def suppress_clustered_drafts(
    drafts: list[dict[str, Any]],
    *,
    cooldown_s: float,
    iou_threshold: float,
    limit: int,
) -> list[dict[str, Any]]:
    """按分数保留分散候选，抑制同一爆点附近的重复节点。"""
    if limit <= 0:
        return []
    selected: list[dict[str, Any]] = []
    for draft in sorted(drafts, key=lambda item: float(item.get("highlight_score", 0.0)), reverse=True):
        peak = _as_datetime(draft.get("peak_ts"))
        start = _as_datetime(draft.get("start_ts"))
        end = _as_datetime(draft.get("end_ts"))
        if peak is None or start is None or end is None:
            continue
        clustered = False
        for existing in selected:
            existing_peak = _as_datetime(existing.get("peak_ts"))
            existing_start = _as_datetime(existing.get("start_ts"))
            existing_end = _as_datetime(existing.get("end_ts"))
            if existing_peak is None or existing_start is None or existing_end is None:
                continue
            peak_gap = datetime_distance_s(peak, existing_peak)
            overlap = interval_iou(start, end, existing_start, existing_end)
            if peak_gap < max(0.0, cooldown_s) or overlap >= iou_threshold:
                clustered = True
                break
        if not clustered:
            selected.append(draft)
        if len(selected) >= limit:
            break
    return sorted(selected, key=lambda item: _datetime_sort_key(_as_datetime(item.get("peak_ts"))))


def interval_iou(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> float:
    """计算两个时间区间的交并比。"""
    start_epoch = datetime_epoch(start)
    end_epoch = datetime_epoch(end)
    other_start_epoch = datetime_epoch(other_start)
    other_end_epoch = datetime_epoch(other_end)
    intersection = max(0.0, min(end_epoch, other_end_epoch) - max(start_epoch, other_start_epoch))
    union = max(0.0, max(end_epoch, other_end_epoch) - min(start_epoch, other_start_epoch))
    return intersection / union if union else 0.0


def datetime_epoch(value: datetime) -> float:
    """按 UTC 语义把 ORM 的无时区时间与 API 的有时区时间统一成秒数。"""
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return normalized.timestamp()


def datetime_distance_s(left: datetime, right: datetime) -> float:
    """返回两个可能混合时区表示的时间点距离。"""
    return abs(datetime_epoch(left) - datetime_epoch(right))


def _clean_danmaku(content: str | None) -> str:
    """规范化用于频次统计的弹幕文本。"""
    value = re.sub(r"\s+", " ", str(content or "")).strip()
    if not value or len(value) > 120:
        return ""
    return value


def _reaction_score(text: str) -> float:
    """返回短促情绪反应强度；零表示不是 reaction。"""
    normalized = text.casefold().replace("？", "?").replace("！", "!")
    if normalized in _REACTION_WORDS:
        return 1.0
    if len(normalized) <= 12 and _REACTION_ONLY_PATTERN.fullmatch(normalized):
        return min(1.0, 0.55 + len(normalized) / 24.0)
    return 0.0


def _information_score(text: str) -> float:
    """估计事件主题承载量，并抑制纯标点/复读反应。"""
    compact = re.sub(r"\s+", "", text)
    semantic = [char for char in compact if char.isalnum() or "\u4e00" <= char <= "\u9fff"]
    if len(semantic) < 4 or (_reaction_score(text) > 0.0 and len(semantic) <= 8):
        return 0.0
    counts = Counter(semantic)
    repetition = max(counts.values()) / len(semantic)
    unique_ratio = len(counts) / len(semantic)
    length_score = min(1.0, len(semantic) / 24.0)
    token_count = len(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", text))
    token_score = min(1.0, token_count / 4.0)
    return _clamp(length_score * 0.45 + unique_ratio * 0.35 + token_score * 0.20 - repetition * 0.15)


def _humor_score(text: str) -> float:
    """返回可选幽默代表强度。"""
    hits = sum(token in text for token in _HUMOR_WORDS)
    if hits == 0:
        return 0.0
    return _clamp(0.45 + hits * 0.15 + _information_score(text) * 0.40)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _datetime_sort_key(value: datetime | None) -> float:
    """把有/无时区时间统一成数值排序键，避免混合比较抛出 TypeError。"""
    if value is None:
        return float("-inf")
    return datetime_epoch(value)
