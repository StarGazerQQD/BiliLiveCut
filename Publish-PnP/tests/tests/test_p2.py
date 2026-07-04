"""P2 单元测试:合集逻辑 + 房间配置。"""

from __future__ import annotations

from app.pipeline.collection import detect_overlap, _generate_chapter_card  # noqa: F811
from app.analysis.room_config import (
    apply_aliases,
    is_blocked_topic,
    load_room_config,
    match_extra_keywords,
)


# ======================== 重合检测 ========================

class TestDetectOverlap:
    """检测相邻事件是否重叠或时间接近。"""

    @staticmethod
    def test_no_overlap() -> None:
        """两个不相邻事件无重叠。"""
        events = [
            {"start_ts": "2026-01-01T00:01:00", "end_ts": "2026-01-01T00:01:30"},
            {"start_ts": "2026-01-01T00:05:00", "end_ts": "2026-01-01T00:05:30"},
        ]
        overlaps = detect_overlap(events)
        assert len(overlaps) == 1
        assert overlaps[0]["mergeable"] is False

    @staticmethod
    def test_overlapping_events() -> None:
        """重叠事件。"""
        events = [
            {"start_ts": "2026-01-01T00:01:00", "end_ts": "2026-01-01T00:01:30"},
            {"start_ts": "2026-01-01T00:01:20", "end_ts": "2026-01-01T00:01:50"},
        ]
        overlaps = detect_overlap(events)
        assert len(overlaps) == 1
        assert overlaps[0]["mergeable"] is True
        assert overlaps[0]["overlap_s"] == 10.0

    @staticmethod
    def test_close_events_mergeable() -> None:
        """时间接近(<=2s 间隙)。"""
        events = [
            {"start_ts": "2026-01-01T00:01:00", "end_ts": "2026-01-01T00:01:30"},
            {"start_ts": "2026-01-01T00:01:31", "end_ts": "2026-01-01T00:02:00"},
        ]
        overlaps = detect_overlap(events, threshold_s=2.0)
        assert len(overlaps) == 1
        assert overlaps[0]["mergeable"] is True

    @staticmethod
    def test_far_events_not_mergeable() -> None:
        """时间差距过大。"""
        events = [
            {"start_ts": "2026-01-01T00:01:00", "end_ts": "2026-01-01T00:01:30"},
            {"start_ts": "2026-01-01T00:10:00", "end_ts": "2026-01-01T00:10:30"},
        ]
        overlaps = detect_overlap(events)
        assert len(overlaps) == 1
        assert overlaps[0]["mergeable"] is False

    @staticmethod
    def test_three_events_middle_overlap() -> None:
        """三个事件,中间都与第一个重叠但第三个与第二个不重叠。"""
        events = [
            {"start_ts": "2026-01-01T00:01:00", "end_ts": "2026-01-01T00:01:30"},
            {"start_ts": "2026-01-01T00:01:20", "end_ts": "2026-01-01T00:01:50"},
            {"start_ts": "2026-01-01T00:05:00", "end_ts": "2026-01-01T00:05:30"},
        ]
        overlaps = detect_overlap(events)
        assert len(overlaps) == 2
        assert overlaps[0]["mergeable"] is True  # 0 ↔ 1
        assert overlaps[1]["mergeable"] is False  # 1 ↔ 2

    @staticmethod
    def test_missing_timestamps() -> None:
        """缺少时间戳的事件。"""
        events = [
            {"start_ts": "2026-01-01T00:01:00", "end_ts": None},
            {"start_ts": None, "end_ts": "2026-01-01T00:01:30"},
        ]
        overlaps = detect_overlap(events)
        assert len(overlaps) == 1
        assert overlaps[0]["mergeable"] is False


# ======================== 房间配置 ========================

class TestRoomConfig:
    """房间配置加载和工具函数。"""

    @staticmethod
    def test_load_defaults_when_none() -> None:
        """无配置时返回默认值。"""
        cfg = load_room_config(None)
        assert cfg["hotwords"] == []
        assert cfg["aliases"] == {}
        assert cfg["highlight_keywords"] == []
        assert cfg["blocked_topics"] == []

    @staticmethod
    def test_load_defaults_when_empty_json() -> None:
        """空 JSON 时返回默认值。"""
        class FakeRoom:
            room_config_json = "{}"

        cfg = load_room_config(FakeRoom())  # type: ignore[arg-type]
        assert cfg["hotwords"] == []

    @staticmethod
    def test_load_valid_json() -> None:
        """加载完整配置。"""
        class FakeRoom:
            room_config_json = '{"hotwords":["审判","翻盘"],"aliases":{"thp":"审判"},"highlight_keywords":["名场面"],"blocked_topics":["广告"]}'

        cfg = load_room_config(FakeRoom())  # type: ignore[arg-type]
        assert cfg["hotwords"] == ["审判", "翻盘"]
        assert cfg["aliases"] == {"thp": "审判"}
        assert cfg["highlight_keywords"] == ["名场面"]
        assert cfg["blocked_topics"] == ["广告"]

    @staticmethod
    def test_load_invalid_json_falls_back() -> None:
        """无效 JSON 回退默认。"""
        class FakeRoom:
            room_config_json = "{bad json"

        cfg = load_room_config(FakeRoom())  # type: ignore[arg-type]
        assert cfg["hotwords"] == []


class TestApplyAliases:
    """别名替换。"""

    @staticmethod
    def test_simple_replacement() -> None:
        """简单替换。"""
        text = "thp 操作真强"
        aliases = {"thp": "审判"}
        result = apply_aliases(text, aliases)
        assert "审判" in result

    @staticmethod
    def test_empty_aliases() -> None:
        """空别名不变。"""
        text = "thp 操作真强"
        result = apply_aliases(text, {})
        assert result == text

    @staticmethod
    def test_case_insensitive() -> None:
        """大小写不敏感。"""
        text = "THP 真强"
        aliases = {"thp": "审判"}
        result = apply_aliases(text, aliases)
        assert "审判" in result


class TestExtraKeywords:
    """额外关键词匹配。"""

    @staticmethod
    def test_hit() -> None:
        """命中关键词。"""
        hits = match_extra_keywords("这个名场面太经典了", ["名场面", "破防"])
        assert hits == ["名场面"]

    @staticmethod
    def test_no_hit() -> None:
        """未命中。"""
        hits = match_extra_keywords("日常聊天", ["名场面", "破防"])
        assert hits == []

    @staticmethod
    def test_empty_keywords() -> None:
        """空关键词列表。"""
        hits = match_extra_keywords("随便说说", [])
        assert hits == []


class TestBlockedTopics:
    """屏蔽话题检测。"""

    @staticmethod
    def test_blocked() -> None:
        """命中屏蔽话题。"""
        assert is_blocked_topic("直播间广告推广", ["广告", "引流"]) is True

    @staticmethod
    def test_not_blocked() -> None:
        """未命中。"""
        assert is_blocked_topic("正常游戏内容", ["广告", "引流"]) is False

    @staticmethod
    def test_empty_blocked() -> None:
        """空屏蔽列表。"""
        assert is_blocked_topic("任何内容", []) is False


# ======================== 文案生成(回退) ========================

class TestFallbackCopywriter:
    """规则回退文案生成。"""

    @staticmethod
    def test_generates_all_fields() -> None:
        """验证回退生成所有必要字段。"""
        from app.publishing.collection_copywriter import _fallback_copywriter

        events = [
            {"score": 0.85, "reason": "弹幕突增", "duration_s": 30},
            {"score": 0.72, "reason": "主播反应", "duration_s": 45},
        ]
        result = _fallback_copywriter("审判翻盘合集", events, 75.0)

        assert "summary" in result
        assert "title" in result
        assert "description" in result
        assert "chapters" in result
        assert "tags" in result
        assert "cover_title" in result
        assert len(result["chapters"]) == 2
        assert "审判" in result["title"]

    @staticmethod
    def test_empty_title() -> None:
        """空主题标题也能生成。"""
        from app.publishing.collection_copywriter import _fallback_copywriter

        result = _fallback_copywriter("", [{"score": 0.5, "duration_s": 10}], 10.0)
        assert result["title"] != ""

    @staticmethod
    def test_sec_to_hhmmss() -> None:
        """秒转时间格式。"""
        from app.publishing.collection_copywriter import sec_to_hhmmss

        assert sec_to_hhmmss(0) == "00:00:00"
        assert sec_to_hhmmss(65) == "00:01:05"
        assert sec_to_hhmmss(3661) == "01:01:01"

    @staticmethod
    def test_chapter_ts_accumulates() -> None:
        """章节时间戳正确累积。"""
        from app.publishing.collection_copywriter import _fallback_copywriter

        events = [
            {"score": 0.8, "duration_s": 30},
            {"score": 0.7, "duration_s": 45},
            {"score": 0.6, "duration_s": 15},
        ]
        result = _fallback_copywriter("合集", events, 90)
        chapters = result["chapters"]
        assert len(chapters) == 3
        assert chapters[0]["ts"] == "00:00:00"
        assert chapters[1]["ts"] == "00:00:30"
        assert chapters[2]["ts"] == "00:01:15"
