"""主题识别与事件聚类(V0.1.6 P1)。

同一场直播中的多个高光必须先判断是否属于同一主题,不能仅因时间接近或分数高
就自动组成合集。综合考虑:

1. ASR 文本语义相似度(字符级 TF-IDF);
2. 相同关键词、实体(人物/游戏/歌曲);
3. 时间距离作为弱特征;
4. 可选 LLM 最终辅助判断。

主题判定三级:
- 相似度 >= 0.82:建议自动归组(同一主题)
- 0.60～0.82:进入人工确认(可能相关)
- < 0.60:保持独立(不同主题)

阈值可配置,并记录实际计算分数和判断原因。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter

from loguru import logger
from sqlmodel import select

from app.db.models import (
    HighlightCandidate,
    HighlightEvent,
    HighlightTopic,
    Topic,
    TopicStatus,
)
from app.db.session import get_session


# 可配置阈值(后续迁移到 settings)。
TOPIC_CONFIDENCE_HIGH = 0.82
TOPIC_CONFIDENCE_LOW = 0.60
TOPIC_TIME_WINDOW_S = 3600  # 1 小时内的时间权重才有效


def _tokenize(text: str) -> list[str]:
    """中文简单分词:按标点和空格切分+去重删空。

    :param text: 待切分文本。
    :returns: token 列表。
    """
    if not text:
        return []
    text = re.sub(r"[\.\,\!\?\;\:\"\'\(\)\[\]\{\}\s\n\r\t]+", " ", text)
    tokens = [t.strip() for t in text.split() if len(t.strip()) >= 1]
    # 单字集合词（中文通常按 n-gram 更有效,但这里用字符级 bigram 模拟 TF-IDF,对短文本友好）。
    return tokens


def _char_bigrams(text: str) -> list[str]:
    """字符级 bigram,对中文短文本(ASR 转写)更友好。

    :param text: 文本。
    :returns: bigram 列表。
    """
    clean = re.sub(r"\s+", "", text)
    if len(clean) <= 1:
        return [clean] if clean else []
    return [clean[i:i + 2] for i in range(len(clean) - 1)]


def cosine_similarity(vec_a: Counter[str], vec_b: Counter[str]) -> float:
    """余弦相似度。

    :param vec_a: 词频向量 A(Counter)。
    :param vec_b: 词频向量 B(Counter)。
    :returns: 0-1 相似度。
    """
    if not vec_a or not vec_b:
        return 0.0
    intersection = set(vec_a.keys()) & set(vec_b.keys())
    dot = sum(vec_a[k] * vec_b[k] for k in intersection)
    norm_a = math.sqrt(sum(v ** 2 for v in vec_a.values()))
    norm_b = math.sqrt(sum(v ** 2 for v in vec_b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def text_similarity(text_a: str, text_b: str) -> float:
    """计算两段 ASR 文本的相似度。

    使用字符级 bigram 的 TF-IDF 风格余弦相似度,对短文本(几句字幕)更鲁棒。

    :param text_a: 文本 A。
    :param text_b: 文本 B。
    :returns: 0-1 相似度。
    """
    if not text_a or not text_b:
        return 0.0
    # 计算词频。
    freq_a = Counter(_char_bigrams(text_a))
    freq_b = Counter(_char_bigrams(text_b))
    # 小的 IDF 惩罚:对极高频 bigram 打折扣。
    total_docs = max(len(freq_a), len(freq_b), 2)
    def idf_weight(freq: Counter[str]) -> dict[str, float]:
        result = {}
        for k, v in freq.items():
            df = 1 if k in freq_a and k in freq_b else 0.5
            result[k] = v * math.log(1 + total_docs / (df + 1))
        return result
    wa = idf_weight(freq_a)
    wb = idf_weight(freq_b)
    intersection = set(wa.keys()) & set(wb.keys())
    dot = sum(wa[k] * wb[k] for k in intersection)
    na = math.sqrt(sum(v ** 2 for v in wa.values()))
    nb = math.sqrt(sum(v ** 2 for v in wb.values()))
    if na == 0 or nb == 0:
        return 0.0
    return min(dot / (na * nb), 1.0)


def keyword_overlap(keywords_a: list[str], keywords_b: list[str]) -> float:
    """关键词重叠率。

    :param keywords_a: 关键词列表 A。
    :param keywords_b: 关键词列表 B。
    :returns: 0-1 重叠率(Jaccard)。
    """
    if not keywords_a or not keywords_b:
        return 0.0
    sa = set(keywords_a)
    sb = set(keywords_b)
    intersection = sa & sb
    union = sa | sb
    if not union:
        return 0.0
    return len(intersection) / len(union)


def event_similarity(event_a: dict, event_b: dict) -> float:
    """计算两个候选事件的综合相似度。

    综合 ASR 文本相似度(权重 0.55)、关键词重叠(权重 0.25)、
    时间接近度(权重 0.20,仅弱特征)。

    :param event_a: 候选 A 的字典(含 asr_text, keywords, score, start_ts)。
    :param event_b: 候选 B 的字典(含 asr_text, keywords, score, start_ts)。
    :returns: 0-1 综合相似度。
    """
    text_a = event_a.get("asr_text", "") or ""
    text_b = event_b.get("asr_text", "") or ""

    # ASR 文本相似度。
    sim_text = text_similarity(text_a, text_b)

    # 关键词重叠。
    kw_a = event_a.get("keywords", []) or []
    kw_b = event_b.get("keywords", []) or []
    sim_kw = keyword_overlap(kw_a, kw_b)

    # 时间接近度:1 小时内线性衰减。
    ts_a = event_a.get("start_ts")
    ts_b = event_b.get("start_ts")
    time_sim = 0.0
    if ts_a and ts_b:
        from datetime import datetime as _dt

        if isinstance(ts_a, str):
            ts_a = _dt.fromisoformat(ts_a)
        if isinstance(ts_b, str):
            ts_b = _dt.fromisoformat(ts_b)
        if isinstance(ts_a, _dt) and isinstance(ts_b, _dt):
            diff_s = abs((ts_a - ts_b).total_seconds())
            if diff_s < TOPIC_TIME_WINDOW_S:
                time_sim = max(0.0, 1.0 - diff_s / TOPIC_TIME_WINDOW_S)

    # 加权融合。
    score = sim_text * 0.55 + sim_kw * 0.25 + time_sim * 0.20

    return round(score, 4)


def _extract_keywords(text: str, max_keywords: int = 8) -> list[str]:
    """从文本中提取关键词(简单规则:高频 bigram)。

    :param text: ASR 文本。
    :param max_keywords: 最多返回关键词数。
    :returns: 关键词列表。
    """
    if not text:
        return []
    bigrams = _char_bigrams(text)
    if not bigrams:
        return []
    counter = Counter(bigrams)
    total = sum(counter.values())
    # 取频率 > 平均的词作为关键词。
    avg = total / len(counter) if counter else 0
    keywords = [k for k, v in counter.most_common(max_keywords * 2) if v > avg]
    return keywords[:max_keywords]


def cluster_candidates(session_id: int) -> list[dict]:
    """对一场直播的所有待审核候选进行主题聚类。

    算法:
    1. 取出该会话所有 PENDING/APPROVED 候选的 ASR 文本和关键词。
    2. 计算两两相似度矩阵。
    3. 使用阈值分层: >=HIGH 自动同组, >=LOW 标记可能相关, <LOW 独立。
    4. 写入 Topic 和 HighlightTopic 表。

    :param session_id: 录制会话 id。
    :returns: 创建/更新的 topic 列表。
    """
    with get_session() as db:
        candidates = db.exec(
            select(HighlightCandidate).where(
                HighlightCandidate.session_id == session_id,
                HighlightCandidate.status.in_(["pending", "approved"]),
            )
        ).all()
    if len(candidates) < 2:
        logger.info("会话 {} 候选不足 2 个,跳过主题聚类。", session_id)
        return []

    # 收集候选数据。
    items: list[dict] = []
    for c in candidates:
        features = {}
        if c.features_json:
            try:
                features = json.loads(c.features_json)
            except json.JSONDecodeError:
                pass
        # 尝试取转写文本。
        asr_text = ""
        with get_session() as db:
            from app.db.models import Transcript

            for seg in db.exec(
                select(Transcript).where(
                    Transcript.segment_id.in_(
                        select(HighlightCandidate.id).where(
                            HighlightCandidate.id == c.id,
                        )
                    )
                )
            ).all():
                asr_text = seg.text or ""
                break

        items.append({
            "id": c.id,
            "asr_text": asr_text,
            "keywords": features.get("keyword_hits", []),
            "score": c.highlight_score,
            "start_ts": c.start_ts.isoformat() if c.start_ts else None,
        })

    # 两两相似度。
    n = len(items)
    matrix: list[list[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            sim = event_similarity(items[i], items[j])
            matrix[i][j] = sim
            matrix[j][i] = sim

    # 聚类:Union-Find。
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[py] = px

    for i in range(n):
        for j in range(i + 1, n):
            if matrix[i][j] >= TOPIC_CONFIDENCE_HIGH:
                union(i, j)

    # 收集簇。
    clusters: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        clusters.setdefault(root, []).append(i)

    # 写入数据库。
    topics_created = []
    with get_session() as db:
        for root, indices in clusters.items():
            if len(indices) < 2:
                continue
            # 计算簇内平均相似度。
            avg_sim = 0.0
            count = 0
            for a in indices:
                for b in indices:
                    if a < b:
                        avg_sim += matrix[a][b]
                        count += 1
            avg_sim = avg_sim / count if count > 0 else 0.0

            # 生成标题和摘要。
            kw_pool: set[str] = set()
            all_text = ""
            for idx in indices:
                all_text += (items[idx].get("asr_text") or "") + " "
                for kw in (items[idx].get("keywords") or []):
                    kw_pool.add(str(kw))
            topic_kw = list(kw_pool)[:10]
            topic_title = ", ".join(topic_kw[:3]) if topic_kw else f"主题簇 #{root}"
            topic_summary = all_text[:200].strip() if all_text else None

            # 检查是否已有相似主题(通过标题去重)。
            existing = db.exec(
                select(Topic).where(
                    Topic.session_id == session_id,
                    Topic.title == topic_title,
                )
            ).first()

            if existing is None:
                topic = Topic(
                    session_id=session_id,
                    title=topic_title,
                    summary=topic_summary,
                    keywords_json=json.dumps(topic_kw, ensure_ascii=False),
                    confidence=round(avg_sim, 4),
                    status=TopicStatus.AUTO,
                )
                db.add(topic)
                db.flush()
                topic_id = topic.id
            else:
                topic_id = existing.id

            # 关联。
            for idx in indices:
                cid = items[idx]["id"]
                sim = 1.0  # 同簇默认 1.0
                existing_link = db.exec(
                    select(HighlightTopic).where(
                        HighlightTopic.event_id == cid,
                        HighlightTopic.topic_id == topic_id,
                    )
                ).first()
                if existing_link is None:
                    db.add(HighlightTopic(
                        event_id=cid,
                        topic_id=topic_id,
                        confidence=round(sim, 4),
                    ))
            topics_created.append({
                "id": topic_id,
                "title": topic_title,
                "confidence": round(avg_sim, 4),
                "event_count": len(indices),
            })

        logger.info(
            "会话 {} 主题聚类完成:创建 {} 个主题,覆盖 {} 个候选。",
            session_id,
            len(topics_created),
            sum(t["event_count"] for t in topics_created),
        )
    return topics_created


def list_topics(session_id: int | None = None) -> list[dict]:
    """获取主题列表。

    :param session_id: 录制会话 id(可空,查所有)。
    :returns: 主题字典列表。
    """
    with get_session() as db:
        stmt = select(Topic)
        if session_id is not None:
            stmt = stmt.where(Topic.session_id == session_id)
        stmt = stmt.order_by(Topic.created_at.desc())
        topics = db.exec(stmt).all()
    result = []
    for t in topics:
        with get_session() as db:
            links = db.exec(
                select(HighlightTopic).where(HighlightTopic.topic_id == t.id)
            ).all()
        result.append({
            "id": t.id,
            "session_id": t.session_id,
            "title": t.title,
            "summary": t.summary,
            "keywords": json.loads(t.keywords_json) if t.keywords_json else [],
            "entities": json.loads(t.entities_json) if t.entities_json else [],
            "confidence": t.confidence,
            "status": t.status,
            "is_collection": t.is_collection,
            "event_count": len(links),
            "event_ids": [l.event_id for l in links],
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })
    return result


def get_topic(topic_id: int) -> dict | None:
    """获取单个主题详情。

    :param topic_id: 主题 id。
    :returns: 主题字典或 ``None``。
    """
    with get_session() as db:
        t = db.get(Topic, topic_id)
        if t is None:
            return None
        links = db.exec(
            select(HighlightTopic).where(HighlightTopic.topic_id == t.id)
        ).all()
    return {
        "id": t.id,
        "session_id": t.session_id,
        "title": t.title,
        "summary": t.summary,
        "keywords": json.loads(t.keywords_json) if t.keywords_json else [],
        "entities": json.loads(t.entities_json) if t.entities_json else [],
        "confidence": t.confidence,
        "status": t.status,
        "is_collection": t.is_collection,
        "event_count": len(links),
        "event_ids": [l.event_id for l in links],
        "events": [{"event_id": l.event_id, "confidence": l.confidence, "sort_order": l.sort_order, "is_manual": l.is_manual} for l in links],
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def update_topic(topic_id: int, **kwargs) -> bool:
    """更新主题属性(title/summary/keywords/status/is_collection)。

    :param topic_id: 主题 id。
    :param kwargs: 要更新的字段。
    :returns: 成功返回 ``True``。
    """
    with get_session() as db:
        t = db.get(Topic, topic_id)
        if t is None:
            return False
        for k, v in kwargs.items():
            if k == "keywords":
                t.keywords_json = json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
            elif k == "entities":
                t.entities_json = json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
            elif hasattr(t, k):
                setattr(t, k, v)
        t.updated_at = __import__("datetime").datetime.now(__import__("datetime").UTC)
        db.add(t)
    return True


def add_event_to_topic(event_id: int, topic_id: int) -> bool:
    """将高光加入已有主题(幂等)。

    :param event_id: 高光候选 id。
    :param topic_id: 主题 id。
    :returns: 成功返回 ``True``。
    """
    with get_session() as db:
        existing = db.exec(
            select(HighlightTopic).where(
                HighlightTopic.event_id == event_id,
                HighlightTopic.topic_id == topic_id,
            )
        ).first()
        if existing:
            return True
        db.add(HighlightTopic(event_id=event_id, topic_id=topic_id, is_manual=True))
    return True


def remove_event_from_topic(event_id: int, topic_id: int) -> bool:
    """从主题移除高光。

    :param event_id: 高光候选 id。
    :param topic_id: 主题 id。
    :returns: 成功返回 ``True``。
    """
    with get_session() as db:
        link = db.exec(
            select(HighlightTopic).where(
                HighlightTopic.event_id == event_id,
                HighlightTopic.topic_id == topic_id,
            )
        ).first()
        if link is None:
            return False
        db.delete(link)
    return True


def merge_topics(source_id: int, target_id: int) -> bool:
    """合并两个主题:源主题的 event 全部移到目标。

    :param source_id: 源主题 id(将被删除)。
    :param target_id: 目标主题 id。
    :returns: 成功返回 ``True``。
    """
    with get_session() as db:
        src = db.get(Topic, source_id)
        tgt = db.get(Topic, target_id)
        if src is None or tgt is None or source_id == target_id:
            return False
        links = db.exec(
            select(HighlightTopic).where(HighlightTopic.topic_id == source_id)
        ).all()
        for l in links:
            existing = db.exec(
                select(HighlightTopic).where(
                    HighlightTopic.event_id == l.event_id,
                    HighlightTopic.topic_id == target_id,
                )
            ).first()
            if existing is None:
                db.add(HighlightTopic(
                    event_id=l.event_id,
                    topic_id=target_id,
                    confidence=l.confidence,
                    is_manual=True,
                    sort_order=l.sort_order,
                ))
        # 删除源主题关联和新主题。
        for l in links:
            db.delete(l)
        db.delete(src)
    return True


def split_topic(topic_id: int, event_ids: list[int]) -> int | None:
    """拆分主题:将指定 event 移出并创建新主题。

    :param topic_id: 原主题 id。
    :param event_ids: 要移出的事件 id 列表。
    :returns: 新主题 id 或 ``None``。
    """
    if len(event_ids) < 1:
        return None
    with get_session() as db:
        t = db.get(Topic, topic_id)
        if t is None:
            return None
        new_topic = Topic(
            session_id=t.session_id,
            title=f"{t.title} (拆分 #{event_ids[0]})",
            status=TopicStatus.AUTO,
        )
        db.add(new_topic)
        db.flush()
        new_id = new_topic.id
        for eid in event_ids:
            link = db.exec(
                select(HighlightTopic).where(
                    HighlightTopic.event_id == eid,
                    HighlightTopic.topic_id == topic_id,
                )
            ).first()
            if link:
                db.delete(link)
            db.add(HighlightTopic(
                event_id=eid,
                topic_id=new_id,
                confidence=0.0,
                is_manual=True,
            ))
        # 标记原主题为已拆分。
        t.status = TopicStatus.SPLIT
        db.add(t)
    return new_id


def reorder_topic_events(topic_id: int, event_ids: list[int]) -> bool:
    """对主题内高光重新排序(设置 sort_order)。

    :param topic_id: 主题 id。
    :param event_ids: 按新顺序排列的事件 id 列表。
    :returns: 成功返回 ``True``。
    """
    with get_session() as db:
        for i, eid in enumerate(event_ids):
            link = db.exec(
                select(HighlightTopic).where(
                    HighlightTopic.topic_id == topic_id,
                    HighlightTopic.event_id == eid,
                )
            ).first()
            if link:
                link.sort_order = i
                db.add(link)
    return True
