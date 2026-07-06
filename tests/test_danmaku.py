"""P0 测试: 弹幕基线计算 + 突增评分(V0.1.6)。"""

from __future__ import annotations

from app.analysis.highlight import (
    danmaku_rate_score,
    fuse_scores,
    weighted_rule_score,
)


class TestDanmakuRateScore:
    """Sigmoid 映射评分测试。"""

    def test_high_ratio_tends_to_one(self) -> None:
        """高倍数时趋于 1.0:sigmoid(x=3.2 → 1/(1+e^(-3.2*1.6))。"""
        s = danmaku_rate_score(window_rate=100.0, baseline_rate=2.0, window_count=50)
        # ratio=50, sigmoid=~1.0
        assert s >= 0.99

    def test_equal_rate_low_score(self) -> None:
        """无突增时分数较低。"""
        s = danmaku_rate_score(window_rate=2.0, baseline_rate=2.0, window_count=50)
        # ratio=1, sigmoid(1)=1/(1+e^(-(1-1.8)*1.6) ≈ 1/(1+e^(1.28)) ≈ 0.22
        assert 0.0 <= s <= 0.4

    def test_moderate_spike(self) -> None:
        """3 倍突增应得中等分数。"""
        s = danmaku_rate_score(window_rate=9.0, baseline_rate=3.0, window_count=50)
        # ratio=3, sigmoid(3)=1/(1+e^(-(3-1.8)*1.6)) ≈ 1/(1+e^(-1.92)) ≈ 0.87
        assert 0.6 <= s <= 1.0

    def test_zero_baseline_protection(self) -> None:
        """基线为 0 时使用保护值,不除零。"""
        s = danmaku_rate_score(window_rate=10.0, baseline_rate=0.0, window_count=50)
        assert s == 0.35

    def test_low_volume_returns_zero(self) -> None:
        """低于 min_samples 时返回 0,不放大噪声。"""
        s = danmaku_rate_score(window_rate=0.5, baseline_rate=0.1, window_count=3)
        assert s == 0.0

    def test_window_count_zero_returns_zero(self) -> None:
        """无弹幕时返回 0。"""
        s = danmaku_rate_score(window_rate=0.0, baseline_rate=1.0, window_count=0)
        assert s == 0.0


class TestFuseScores:
    """信任策略融合函数。"""

    def test_close_scores_trust_rule(self) -> None:
        """规则与 LLM 分数接近时偏规则(alpha > beta)。"""
        s = fuse_scores(rule=0.8, llm_score=0.78, alpha=0.6, beta=0.4)
        assert 0.75 <= s <= 0.85

    def test_llm_influence(self) -> None:
        """LLM 有非零 beta 时影响结果。"""
        s = fuse_scores(rule=0.3, llm_score=0.9, alpha=0.5, beta=0.5)
        # (0.5*0.3 + 0.5*0.9) / 1.0 = 0.6
        assert 0.5 <= s <= 0.7

    def test_none_llm(self) -> None:
        """无 LLM 时直接返回规则分。"""
        s = fuse_scores(rule=0.65, llm_score=None, alpha=0.6, beta=0.0)
        assert s == 0.65

    def test_beta_zero_ignores_llm(self) -> None:
        """beta=0 时 LLM 不参与融合。"""
        s = fuse_scores(rule=0.3, llm_score=0.9, alpha=0.6, beta=0.0)
        # (0.6*0.3 + 0*0.9) / 0.6 = 0.3
        assert s == 0.3


class TestWeightedRuleScore:
    """多维加权评分。"""

    def test_all_zero(self) -> None:
        """全部零特征→零评分。"""
        s = weighted_rule_score({"a": 0.0, "b": 0.0}, {"a": 0.5, "b": 0.5})
        assert s == 0.0

    def test_missing_weight_is_zero(self) -> None:
        """不在权重字典中的特征不计入。"""
        s = weighted_rule_score({"a": 0.8, "b": 0.5}, {"a": 0.6})
        # 0.6*0.8 = 0.48, sum_w=0.6 → 0.48/0.6=0.8 (b 不计入权重和)
        assert s == 0.8

    def test_normal_distribution(self) -> None:
        """正常权重分配。"""
        s = weighted_rule_score({"a": 0.9, "b": 0.3}, {"a": 0.7, "b": 0.3})
        assert 0.6 <= s <= 1.0
