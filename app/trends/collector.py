"""网感资料库采集器。

调用具备联网搜索能力的模型,采集近期全网(B 站/抖音/微博热搜等)高热度的
短视频/直播相关话题,产出结构化记录(标题、摘要、标签、热度、题材、链接)。

设计与 :mod:`app.analysis.llm` 一致:LLM 不可用时返回空列表,绝不抛错中断流程。
合规说明:仅采集公开的热门榜单/话题文字信息用于选题与文案参考,不抓取受保护内容。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from loguru import logger

from app.analysis import llm as llm_mod
from app.core.config import settings

# 默认采集主题(可由调用方覆盖)。
_DEFAULT_TOPIC = "B 站、抖音、微博热搜等平台近期(最近几天)适合做直播切片/短视频的高热度话题、热梗、热门内容、热门标签"

_COLLECT_PROMPT = """你是一名资深的短视频/直播内容运营,擅长把握"网感"与热点。\
请联网检索并整理:{topic}。

只关注与短视频/直播二创、切片选题、标题与标签风格相关的高热度内容。

请只输出 JSON 数组(不要任何额外文字),最多 {max_items} 条,每条对象格式:
{{"source": "来源平台(bilibili/douyin/weibo/web 等)", \
"category": "题材分类(如 游戏/知识/生活/影视/二次元/体育/搞笑 等)", \
"title": "话题或代表性标题", \
"summary": "1-2 句概括为什么热、看点是什么", \
"tags": ["相关标签或热词", "..."], \
"heat": 0到100的相对热度数字, \
"url": "可选的来源链接或留空"}}

要求:标题与标签要真实反映当下流行表达;heat 用相对值体现热度高低;尽量覆盖不同题材。"""


@dataclass(slots=True)
class TrendRecord:
    """一条采集到的热门内容记录。

    :param source: 来源平台。
    :param title: 话题/标题。
    :param category: 题材分类(可空)。
    :param summary: 摘要(可空)。
    :param tags: 标签列表。
    :param heat: 相对热度(0-100)。
    :param url: 来源链接(可空)。
    """

    source: str
    title: str
    category: str | None = None
    summary: str | None = None
    tags: list[str] = field(default_factory=list)
    heat: float = 0.0
    url: str | None = None


def _coerce_record(item: object) -> TrendRecord | None:
    """把模型返回的单个 JSON 对象转为 :class:`TrendRecord`(健壮容错)。

    :param item: 单条原始数据(应为 dict)。
    :returns: 解析出的记录;标题缺失或结构异常时返回 ``None``。
    """
    if not isinstance(item, dict):
        return None
    title = str(item.get("title", "")).strip()
    if not title:
        return None
    raw_tags = item.get("tags") or []
    if isinstance(raw_tags, list):
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    else:
        tags = []
    try:
        heat = float(item.get("heat", 0) or 0)
    except (TypeError, ValueError):
        heat = 0.0
    heat = max(0.0, min(heat, 100.0))
    return TrendRecord(
        source=str(item.get("source", "web")).strip() or "web",
        title=title[:200],
        category=(str(item.get("category", "")).strip() or None),
        summary=(str(item.get("summary", "")).strip() or None),
        tags=tags[:12],
        heat=heat,
        url=(str(item.get("url", "")).strip() or None),
    )


def collect_trends(topic: str = "") -> list[TrendRecord]:
    """联网采集近期热门内容并解析为记录列表。

    :param topic: 采集主题提示(留空用默认主题)。
    :returns: :class:`TrendRecord` 列表;LLM 不可用或解析失败时返回空列表。
    """
    if not settings.trend_enabled:
        logger.info("网感资料库未启用(TREND_ENABLED=false),跳过采集。")
        return []

    prompt = _COLLECT_PROMPT.format(
        topic=topic or _DEFAULT_TOPIC,
        max_items=settings.trend_max_items,
    )

    if settings.trend_web_search:
        raw = llm_mod.call_trend_search(
            prompt,
            max_tokens=4096,
            max_searches=settings.trend_max_searches,
        )
    else:
        raw = llm_mod.call_text(prompt, max_tokens=4096)

    if raw is None:
        logger.warning("网感采集未获得模型输出(LLM 不可用或超预算),返回空。")
        return []

    data = llm_mod.extract_json_array(raw)
    if data is None:
        logger.warning("网感采集输出无法解析为 JSON 数组: {}", raw[:200])
        return []

    records = [r for r in (_coerce_record(it) for it in data) if r is not None]
    logger.info("网感采集解析出 {} 条记录。", len(records))
    return records[: settings.trend_max_items]


def collect_and_save(topic: str = "") -> int:
    """采集并写入资料库,返回新增/更新的条目数。

    :param topic: 采集主题提示。
    :returns: 入库(新增或更新)的条目数。
    """
    from app.trends import store

    records = collect_trends(topic)
    if not records:
        return 0
    saved = store.save_trends(records)
    store.purge_old(settings.trend_retention_days)
    return saved


def trend_to_dict(rec: TrendRecord) -> dict:
    """把记录转为可序列化字典(留档用)。

    :param rec: 记录。
    :returns: 字典。
    """
    return {
        "source": rec.source,
        "title": rec.title,
        "category": rec.category,
        "summary": rec.summary,
        "tags": rec.tags,
        "heat": rec.heat,
        "url": rec.url,
    }


def _dumps(obj: object) -> str:
    """紧凑 JSON 序列化(中文不转义)(未使用,保留备用)。

    :param obj: 任意可序列化对象。
    :returns: JSON 字符串。
    """
    return json.dumps(obj, ensure_ascii=False)
