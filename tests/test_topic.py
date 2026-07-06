"""P1 测试: 主题相似度 + 聚类 + 人工管理(V0.1.6)。"""

from __future__ import annotations

import pytest

from app.analysis.topic_cluster import (
    TOPIC_CONFIDENCE_HIGH,
    TOPIC_CONFIDENCE_LOW,
    cosine_similarity,
    event_similarity,
    keyword_overlap,
    text_similarity,
)


class TestTextSimilarity:
    """ASR 文本相似度测试。"""

    def test_identical_text(self) -> None:
        """相同文本得满分。"""
        s = text_similarity("卧槽这波操作绝了", "卧槽这波操作绝了")
        assert s >= 0.95

    def test_similar_meaning(self) -> None:
        """语义相近的文本得分高。"""
        s = text_similarity(
            "主播在第三局陷入巨大劣势队友全部倒下",
            "队友全倒后主播一人完成极限翻盘",
        )
        # 有"队友""主播"等重叠词。
        assert s > 0.1

    def test_unrelated_text(self) -> None:
        """不相关文本得分低。"""
        s = text_similarity(
            "今天天气真好适合出去玩",
            "游戏翻盘残局操作太离谱了",
        )
        assert s < TOPIC_CONFIDENCE_LOW

    def test_empty_text(self) -> None:
        """空文本返回 0。"""
        assert text_similarity("", "abc") == 0.0
        assert text_similarity("abc", "") == 0.0
        assert text_similarity("", "") == 0.0

    def test_single_char(self) -> None:
        """单字文本。"""
        s = text_similarity("啊", "啊")
        assert 0.0 <= s <= 1.0


class TestKeywordOverlap:
    """关键词重叠率测试。"""

    def test_full_overlap(self) -> None:
        """完全相同关键词→重叠率 1.0。"""
        k = keyword_overlap(["翻盘", "残局", "绝杀"], ["翻盘", "残局", "绝杀"])
        assert k == 1.0

    def test_partial_overlap(self) -> None:
        """部分重叠。"""
        k = keyword_overlap(["翻盘", "残局", "绝杀"], ["翻盘", "五杀", "操作"])
        # 交集=1, 并集=5 → 0.2。
        assert 0.1 <= k <= 0.4

    def test_no_overlap(self) -> None:
        """无重叠。"""
        k = keyword_overlap(["翻盘", "残局"], ["唱歌", "跳舞"])
        assert k == 0.0

    def test_empty_keywords(self) -> None:
        """空关键词列表。"""
        assert keyword_overlap([], ["a", "b"]) == 0.0
        assert keyword_overlap(["a", "b"], []) == 0.0


class TestCosineSimilarity:
    """余弦相似度。"""

    def test_same_vector(self) -> None:
        """相同向量→1.0。"""
        from collections import Counter

        a = Counter({"a": 3, "b": 2})
        assert cosine_similarity(a, a) == pytest.approx(1.0)

    def test_orthogonal(self) -> None:
        """正交向量→0。"""
        from collections import Counter

        s = cosine_similarity(Counter({"a": 1}), Counter({"b": 1}))
        assert s == 0.0

    def test_empty(self) -> None:
        """空向量→0。"""
        from collections import Counter

        assert cosine_similarity(Counter(), Counter({"a": 1})) == 0.0


class TestEventSimilarity:
    """事件综合相似度测试。"""

    def test_same_event(self) -> None:
        """同一事件→高相似度。"""
        a = {"asr_text": "卧槽这波操作绝了直接五杀", "keywords": ["五杀", "绝了"], "start_ts": "2026-07-01T12:00:00"}
        b = {"asr_text": "卧槽这波操作绝了直接五杀", "keywords": ["五杀", "绝了"], "start_ts": "2026-07-01T12:00:00"}
        s = event_similarity(a, b)
        assert s >= 0.9

    def test_different_topics(self) -> None:
        """完全不同主题的事件→低于 LOW 阈值。"""
        a = {
            "asr_text": "今天翻盘太刺激了残局一打三",
            "keywords": ["翻盘", "残局", "一打三"],
            "start_ts": "2026-07-01T12:00:00",
        }
        b = {
            "asr_text": "给大家唱一首稻香放松一下心情",
            "keywords": ["唱歌", "稻香", "心情"],
            "start_ts": "2026-07-01T12:30:00",
        }
        s = event_similarity(a, b)
        assert s < TOPIC_CONFIDENCE_LOW

    def test_empty_fields(self) -> None:
        """空字段返回低分。"""
        a = {"asr_text": "", "keywords": [], "start_ts": None}
        b = {"asr_text": "", "keywords": [], "start_ts": None}
        s = event_similarity(a, b)
        assert s == 0.0


class TestThresholdConstants:
    """阈值常量。"""

    def test_high_above_low(self) -> None:
        """HIGH 阈值 > LOW 阈值。"""
        assert TOPIC_CONFIDENCE_HIGH > TOPIC_CONFIDENCE_LOW

    def test_thresholds_in_range(self) -> None:
        """阈值在 0-1 范围内。"""
        assert 0.0 <= TOPIC_CONFIDENCE_LOW <= 1.0
        assert 0.0 <= TOPIC_CONFIDENCE_HIGH <= 1.0
