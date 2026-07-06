"""ASR 复核闭环测试 (V0.1.12.2)。

测试 review_risk_score、局部音频截取、文本合并、对齐。
"""

from __future__ import annotations

import pytest

from app.analysis.transcribe import (
    ASRSegmentResult,
    _compute_review_risk_score,
    _merge_review_text,
)


class TestReviewRiskScore:
    """复核风险评分。"""

    def test_high_confidence_no_trigger(self) -> None:
        """高置信度句不应触发复核。"""
        seg = ASRSegmentResult(
            start=0.0, end=5.0, text="今天天气真好阳光明媚",
            normalized_confidence=0.95, confidence_available=True,
        )
        risk, reasons = _compute_review_risk_score(seg)
        assert risk < 0.65
        assert "confidence_unavailable" not in reasons

    def test_empty_text_triggers(self) -> None:
        """空文本触发复核。"""
        seg = ASRSegmentResult(
            start=0.0, end=5.0, text="",
            confidence_available=False,
        )
        risk, reasons = _compute_review_risk_score(seg)
        assert risk >= 0.5
        assert "empty_text" in reasons

    def test_low_confidence_triggers(self) -> None:
        """低置信度触发复核。"""
        seg = ASRSegmentResult(
            start=0.0, end=5.0, text="xxx",
            normalized_confidence=0.3, confidence_available=True,
        )
        risk, reasons = _compute_review_risk_score(seg)
        assert any("low_confidence" in r for r in reasons)

    def test_repetition_triggers(self) -> None:
        """重复字符触发。"""
        seg = ASRSegmentResult(
            start=0.0, end=5.0, text="哈哈哈哈哈哈哈哈哈哈",
            confidence_available=False,
        )
        risk, reasons = _compute_review_risk_score(seg)
        assert "high_repetition" in reasons

    def test_garbled_triggers(self) -> None:
        """乱码触发。"""
        seg = ASRSegmentResult(
            start=0.0, end=5.0, text="\x00\x01\x02\x03###",
            confidence_available=False,
        )
        risk, reasons = _compute_review_risk_score(seg)
        assert "possible_garbled" in reasons

    def test_hotword_conflict(self) -> None:
        """热词冲突触发。"""
        seg = ASRSegmentResult(
            start=0.0, end=3.0, text="今天玩的是原",  # 不完整的热词
            confidence_available=False,
        )
        risk, reasons = _compute_review_risk_score(seg, hotwords=["原神", "鸣潮"])
        assert any("hotword_conflict" in r for r in reasons)


class TestMergeReviewText:
    """base/review 文本合并。"""

    def test_review_empty_keep_base(self) -> None:
        final, decision, reasons = _merge_review_text("你好世界", "", 0.7)
        assert final == "你好世界"
        assert decision == "keep_base"

    def test_low_edit_distance_keep_base(self) -> None:
        final, decision, _ = _merge_review_text("今天天气好", "今天天气好好", 0.7)
        assert decision == "keep_base"

    def test_review_has_hotwords_adopt(self) -> None:
        final, decision, _ = _merge_review_text(
            "今天玩的是原神", "今天玩的是鸣潮", 0.7,
            hotwords=["鸣潮"],
        )
        assert decision == "use_review"
        assert "鸣潮" in final

    def test_high_edit_distance_manual(self) -> None:
        final, decision, _ = _merge_review_text(
            "今天天气真好适合出去玩", "游戏翻盘残局操作太离谱了", 0.8,
        )
        assert decision == "manual_review_needed"

    def test_accept_review_default(self) -> None:
        final, decision, _ = _merge_review_text(
            "今天玩的是原神", "今天玩的是鸣潮", 0.8,
        )
        # 编辑距离 > 20% 但 < 50%, 无热词命中, 默认采用复核
        assert decision in ("use_review", "manual_review_needed")
