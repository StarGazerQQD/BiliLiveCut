"""网感资料库的持久化与查询。

负责采集结果入库去重、近期热度统计、关键词/标签聚合、与给定文本的关联度评分,
以及过期清理。关联度评分核心是纯函数 :func:`relevance_score`,便于单测。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import timedelta

from loguru import logger
from sqlmodel import select

from app.analysis.speedups import fast_match_keywords
from app.db.models import TrendItem, utcnow
from app.db.session import get_session
from app.trends.collector import TrendRecord, trend_to_dict


def _hash(source: str, title: str) -> str:
    """计算去重指纹(来源+标题)。

    :param source: 来源平台。
    :param title: 标题/话题。
    :returns: SHA1 十六进制串。
    """
    return hashlib.sha1(f"{source}::{title}".strip().lower().encode("utf-8")).hexdigest()


def _keywords_from(rec: TrendRecord) -> list[str]:
    """从记录抽取关键词(标签 + 题材),去重保序。

    :param rec: 采集记录。
    :returns: 关键词列表。
    """
    terms: list[str] = list(rec.tags)
    if rec.category:
        terms.append(rec.category)
    return list(dict.fromkeys(t.strip() for t in terms if t and t.strip()))


def save_trends(records: list[TrendRecord]) -> int:
    """把采集记录写入资料库(按 source+title 去重)。

    已存在的条目:刷新热度、累加 ``seen_count``、更新采集时间并并入新标签;
    新条目:插入。

    :param records: 采集记录列表。
    :returns: 新增或更新的条目数。
    """
    if not records:
        return 0
    now = utcnow()
    saved = 0
    with get_session() as db:
        for rec in records:
            h = _hash(rec.source, rec.title)
            existing = db.exec(select(TrendItem).where(TrendItem.content_hash == h)).first()
            keywords = _keywords_from(rec)
            if existing is not None:
                existing.heat = rec.heat
                existing.heat_peak = max(existing.heat_peak, rec.heat)
                existing.seen_count += 1
                existing.collected_at = now
                if rec.summary:
                    existing.summary = rec.summary
                merged_tags = list(dict.fromkeys(json.loads(existing.tags_json) + rec.tags))
                existing.tags_json = json.dumps(merged_tags, ensure_ascii=False)
                merged_kw = list(dict.fromkeys(json.loads(existing.keywords_json) + keywords))
                existing.keywords_json = json.dumps(merged_kw, ensure_ascii=False)
                db.add(existing)
            else:
                db.add(
                    TrendItem(
                        source=rec.source,
                        category=rec.category,
                        title=rec.title,
                        summary=rec.summary,
                        url=rec.url,
                        tags_json=json.dumps(rec.tags, ensure_ascii=False),
                        keywords_json=json.dumps(keywords, ensure_ascii=False),
                        heat=rec.heat,
                        heat_peak=rec.heat,
                        content_hash=h,
                        first_seen_at=now,
                        collected_at=now,
                        raw_json=json.dumps(trend_to_dict(rec), ensure_ascii=False),
                    )
                )
            saved += 1
    logger.info("网感资料库已写入/更新 {} 条。", saved)
    return saved


def recent_trends(limit: int = 50, days: int | None = None) -> list[TrendItem]:
    """返回最近的资料库条目(按热度降序)。

    :param limit: 数量上限。
    :param days: 仅取最近 N 天内采集的条目(``None`` 表示不限)。
    :returns: :class:`TrendItem` 列表。
    """
    with get_session() as db:
        stmt = select(TrendItem)
        if days is not None:
            cutoff = utcnow() - timedelta(days=days)
            stmt = stmt.where(TrendItem.collected_at >= cutoff)
        stmt = stmt.order_by(TrendItem.heat.desc())  # type: ignore[attr-defined]
        return list(db.exec(stmt).all()[:limit])


def keyword_heat(days: int = 7, top: int = 30) -> list[dict]:
    """聚合近期标签/关键词的热度与出现次数。

    :param days: 近期窗口(天)。
    :param top: 返回前 N 个。
    :returns: ``[{"keyword", "heat", "count"}, ...]``(按热度降序)。
    """
    items = recent_trends(limit=10_000, days=days)
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"heat": 0.0, "count": 0.0})
    for it in items:
        for kw in json.loads(it.keywords_json or "[]"):
            agg[kw]["heat"] += it.heat
            agg[kw]["count"] += 1
    ranked = sorted(agg.items(), key=lambda kv: kv[1]["heat"], reverse=True)
    return [{"keyword": k, "heat": round(v["heat"], 1), "count": int(v["count"])} for k, v in ranked[:top]]


def relevance_score(text: str, term_weights: list[tuple[str, float]]) -> tuple[float, list[str]]:
    """计算文本与一组带权热词的关联度(纯函数)——V0.1.9 Aho-Corasick 加速。

    每个在文本中出现的热词贡献其权重(0-1);累计贡献约 2.0 即视为强相关(满分)。

    V0.1.9: 使用一次 AC 扫描替代逐词 ``in`` 循环,20-50× 加速。

    :param text: 待评估文本(如片段转写)。
    :param term_weights: ``[(热词, 权重0-1), ...]``。
    :returns: ``(score, matched_terms)``,``score`` 为 0-1。
    """
    if not text or not term_weights:
        return 0.0, []
    terms = tuple(t.strip() for t, _ in term_weights if len(t.strip()) >= 2)
    if not terms:
        return 0.0, []
    hits = fast_match_keywords(text.lower(), terms)
    if not hits:
        return 0.0, []
    # 按权重聚合并去除重复命中。
    weight_map = {t.strip(): float(w) for t, w in term_weights if len(t.strip()) >= 2}
    matched: dict[str, float] = {}
    for hit in hits:
        w = weight_map.get(hit, 0.0)
        matched[hit] = max(matched.get(hit, 0.0), w)
    score = min(sum(matched.values()) / 2.0, 1.0)
    ordered = sorted(matched, key=lambda k: matched[k], reverse=True)
    return float(score), ordered


def match_text(text: str, days: int = 7) -> tuple[float, list[str]]:
    """计算文本与资料库近期热门内容的关联度。

    :param text: 待评估文本。
    :param days: 近期窗口(天)。
    :returns: ``(score, matched_terms)``。
    """
    if not text:
        return 0.0, []
    items = recent_trends(limit=500, days=days)
    term_weights: list[tuple[str, float]] = []
    for it in items:
        weight = max(0.0, min(it.heat / 100.0, 1.0))
        for kw in json.loads(it.keywords_json or "[]"):
            term_weights.append((kw, weight))
    return relevance_score(text, term_weights)


def style_reference(days: int = 7, top_titles: int = 8, top_tags: int = 12) -> dict:
    """为文案生成提供风格参考:近期热门标题与热门标签。

    :param days: 近期窗口(天)。
    :param top_titles: 标题数量。
    :param top_tags: 标签数量。
    :returns: ``{"titles": [...], "tags": [...]}``。
    """
    items = recent_trends(limit=top_titles, days=days)
    titles = [it.title for it in items]
    tags = [k["keyword"] for k in keyword_heat(days=days, top=top_tags)]
    return {"titles": titles, "tags": tags}


def purge_old(days: int) -> int:
    """删除超过保留期的资料库条目。

    :param days: 保留天数。
    :returns: 删除的条目数。
    """
    cutoff = utcnow() - timedelta(days=days)
    with get_session() as db:
        rows = db.exec(select(TrendItem).where(TrendItem.collected_at < cutoff)).all()
        for r in rows:
            db.delete(r)
        n = len(rows)
    if n:
        logger.info("网感资料库清理过期条目 {} 条(> {} 天)。", n, days)
    return n
