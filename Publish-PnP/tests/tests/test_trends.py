"""网感资料库测试:关联度评分、入库去重、聚合、清理与采集解析。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING

from app.analysis import llm as llm_mod
from app.db.models import TrendItem, utcnow
from app.db.session import get_session
from app.trends import collector as collector_mod
from app.trends import store
from app.trends.collector import TrendRecord, collect_trends

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def test_relevance_score_pure() -> None:
    """命中带权热词应给出正分,无命中为 0,强命中接近满分。"""
    weights = [("吃鸡", 1.0), ("名场面", 0.8), ("无关词", 0.5)]
    score, matched = store.relevance_score("今天这波吃鸡真是名场面", weights)
    assert score > 0.5
    assert "吃鸡" in matched and "名场面" in matched
    assert store.relevance_score("毫不相关的内容", weights) == (0.0, [])
    assert store.relevance_score("", weights) == (0.0, [])


def test_relevance_score_ignores_short_terms() -> None:
    """长度小于 2 的热词应被忽略,避免噪声误命中。"""
    score, matched = store.relevance_score("abc", [("a", 1.0)])
    assert score == 0.0
    assert matched == []


def test_save_and_recent_and_dedupe(temp_db: None) -> None:
    """入库后可查询;同 source+title 再次入库应去重并累加 seen_count。

    :param temp_db: 隔离数据库夹具。
    """
    recs = [
        TrendRecord(
            source="bilibili", title="A话题", category="游戏",
            tags=["吃鸡", "名场面"], heat=80,
        ),
        TrendRecord(source="douyin", title="B话题", category="搞笑", tags=["整活"], heat=50),
    ]
    assert store.save_trends(recs) == 2
    items = store.recent_trends(limit=10, days=7)
    assert len(items) == 2
    assert items[0].title == "A话题"  # 按热度降序

    # 再次入库 A话题(更高热度、新增标签)。
    store.save_trends([
        TrendRecord(source="bilibili", title="A话题", tags=["逆风翻盘"], heat=95),
    ])
    items = store.recent_trends(limit=10, days=7)
    assert len(items) == 2  # 未新增条目
    a = next(it for it in items if it.title == "A话题")
    assert a.seen_count == 2
    assert a.heat == 95
    assert a.heat_peak == 95
    assert "逆风翻盘" in json.loads(a.tags_json)


def test_keyword_heat_aggregation(temp_db: None) -> None:
    """热词聚合应按热度汇总并降序返回。

    :param temp_db: 隔离数据库夹具。
    """
    store.save_trends([
        TrendRecord(source="b", title="t1", tags=["吃鸡"], category="游戏", heat=60),
        TrendRecord(source="b", title="t2", tags=["吃鸡"], category="游戏", heat=40),
        TrendRecord(source="b", title="t3", tags=["整活"], heat=30),
    ])
    kws = store.keyword_heat(days=7, top=10)
    top = {k["keyword"]: k for k in kws}
    assert top["吃鸡"]["count"] == 2
    assert top["吃鸡"]["heat"] == 100.0
    assert kws[0]["keyword"] == "吃鸡"  # 热度最高排第一


def test_match_text_db(temp_db: None) -> None:
    """文本与近期热门标签的关联度应可从库中计算。

    :param temp_db: 隔离数据库夹具。
    """
    store.save_trends([
        TrendRecord(source="b", title="t1", tags=["吃鸡", "名场面"], heat=90),
    ])
    score, matched = store.match_text("这把吃鸡操作太秀了", days=7)
    assert score > 0
    assert "吃鸡" in matched
    assert store.match_text("完全无关的文字", days=7) == (0.0, [])


def test_purge_old(temp_db: None) -> None:
    """超过保留期的条目应被清理。

    :param temp_db: 隔离数据库夹具。
    """
    store.save_trends([TrendRecord(source="b", title="old", tags=["x"], heat=10)])
    with get_session() as db:
        item = db.exec(__import__("sqlmodel").select(TrendItem)).first()
        item.collected_at = utcnow() - timedelta(days=30)
        db.add(item)
    assert store.purge_old(days=14) == 1
    assert store.recent_trends(limit=10) == []


def test_collect_trends_disabled(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """未启用时采集应直接返回空列表,不调用模型。

    :param temp_db: 隔离数据库夹具。
    :param monkeypatch: pytest 夹具。
    """
    monkeypatch.setattr(collector_mod.settings, "trend_enabled", False, raising=False)
    assert collect_trends() == []


def test_collect_trends_parses_mocked_llm(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """启用并 mock 联网搜索返回 JSON 数组时,应解析为记录。

    :param temp_db: 隔离数据库夹具。
    :param monkeypatch: pytest 夹具。
    """
    monkeypatch.setattr(collector_mod.settings, "trend_enabled", True, raising=False)
    monkeypatch.setattr(collector_mod.settings, "trend_web_search", True, raising=False)
    payload = json.dumps([
        {"source": "bilibili", "category": "游戏", "title": "热门话题X",
         "summary": "很火", "tags": ["吃鸡", "名场面"], "heat": 88, "url": ""},
        {"title": ""},  # 无标题,应被丢弃
        "非对象",          # 非 dict,应被丢弃
    ])
    monkeypatch.setattr(llm_mod, "call_web_search", lambda *a, **k: payload)

    recs = collect_trends()
    assert len(recs) == 1
    assert recs[0].title == "热门话题X"
    assert recs[0].heat == 88.0
    assert "吃鸡" in recs[0].tags

    # 入库验证。
    assert store.save_trends(recs) == 1
