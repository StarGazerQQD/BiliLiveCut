"""阶段2 分析逻辑的纯函数单元测试(不依赖模型与网络)。"""

from __future__ import annotations

import numpy as np

from app.analysis.audio import (
    compute_rms_envelope,
    find_silences,
    snap_to_silence,
)
from app.analysis.highlight import (
    fuse_scores,
    laughter_score,
    speech_rate_score,
    temporal_iou,
    weighted_rule_score,
)
from app.analysis.keywords import match_keywords


# --------------------------- 关键词 --------------------------- #
def test_match_keywords_hits_and_score() -> None:
    """命中多个关键词时给出非零分并返回命中列表。"""
    score, hits = match_keywords("这波操作绝了,直接五杀,笑死我了")
    assert score > 0
    assert "绝了" in hits
    assert "五杀" in hits


def test_match_keywords_empty() -> None:
    """空文本得 0 分。"""
    assert match_keywords("") == (0.0, [])


# --------------------------- 规则打分 --------------------------- #
def test_weighted_rule_score_renormalizes() -> None:
    """仅对出现的维度归一化权重。"""
    feats = {"volume": 1.0, "keywords": 0.0}
    weights = {"volume": 0.25, "keywords": 0.25, "danmaku": 0.5}
    # 只用了 volume/keywords,权重各 0.5,结果应为 0.5。
    assert abs(weighted_rule_score(feats, weights) - 0.5) < 1e-6


def test_weighted_rule_score_zero_weights() -> None:
    """权重全 0 时返回 0,避免除零。"""
    assert weighted_rule_score({"x": 1.0}, {"x": 0.0}) == 0.0


def test_speech_rate_score_detects_burst() -> None:
    """局部密集说话应明显高于均匀分布(突发检测)。"""
    # 突发:前 1 秒密集 10 个词,其余稀疏。
    burst = [{"start": i * 0.1} for i in range(10)] + [{"start": 5 + i} for i in range(5)]
    # 均匀:15 个词在 10 秒内均匀分布。
    uniform = [{"start": i * (10 / 15)} for i in range(15)]
    burst_score = speech_rate_score(burst, duration_s=10.0)
    uniform_score = speech_rate_score(uniform, duration_s=10.0)
    assert burst_score > uniform_score
    assert burst_score > 0.2


def test_speech_rate_score_empty() -> None:
    """无词或零时长时得 0。"""
    assert speech_rate_score([], 10.0) == 0.0
    assert speech_rate_score([{"start": 0}], 0.0) == 0.0


def test_laughter_score() -> None:
    """笑声拟声字越多分越高,且封顶为 1。"""
    assert laughter_score("哈哈哈哈哈哈哈哈") == 1.0
    assert laughter_score("正常文本") == 0.0


def test_fuse_scores() -> None:
    """无 LLM 分时返回规则分;有则按系数融合。"""
    assert fuse_scores(0.8, None, 0.5, 0.5) == 0.8
    assert abs(fuse_scores(0.6, 1.0, 0.5, 0.5) - 0.8) < 1e-6


def test_temporal_iou() -> None:
    """IoU 计算正确:完全重叠=1,无重叠=0。"""
    assert temporal_iou((0, 10), (0, 10)) == 1.0
    assert temporal_iou((0, 10), (20, 30)) == 0.0
    assert abs(temporal_iou((0, 10), (5, 15)) - (5 / 15)) < 1e-6


# --------------------------- 音频包络/静音 --------------------------- #
def test_compute_rms_envelope_normalized() -> None:
    """RMS 包络归一化到峰值为 1。"""
    sr = 16000
    # 1 秒静音 + 1 秒正弦波。
    t = np.linspace(0, 1, sr, endpoint=False)
    tone = 0.5 * np.sin(2 * np.pi * 440 * t)
    pcm = np.concatenate([np.zeros(sr), tone]).astype(np.float32)
    times, rms = compute_rms_envelope(pcm, sr, hop_s=0.1)
    assert rms.size > 0
    assert abs(float(np.max(rms)) - 1.0) < 1e-6
    # 前半段应接近静音。
    assert float(np.mean(rms[:5])) < 0.1


def test_find_silences_detects_quiet_region() -> None:
    """能在包络中找到一段静音区间。"""
    times = np.arange(0, 5, 0.1)
    rms = np.ones_like(times)
    rms[10:30] = 0.0  # 1.0s~3.0s 静音
    silences = find_silences(times, rms, threshold_ratio=0.15, min_silence_s=0.3)
    assert len(silences) == 1
    start, end = silences[0]
    assert 0.9 <= start <= 1.1
    assert 2.8 <= end <= 3.1


def test_snap_to_silence() -> None:
    """目标点会吸附到最近静音中点;过远则保持原值。"""
    silences = [(9.0, 11.0)]  # 中点 10.0
    assert snap_to_silence(9.5, silences, max_shift_s=5.0) == 10.0
    # 距离过远(>5s)则不动。
    assert snap_to_silence(2.0, silences, max_shift_s=5.0) == 2.0
    # 无静音时原样返回。
    assert snap_to_silence(7.0, [], max_shift_s=5.0) == 7.0


def test_scoring_config_defaults() -> None:
    """评分配置可加载,且含必要字段。"""
    from app.analysis.scoring_config import get_scoring_config

    cfg = get_scoring_config()
    assert "volume" in cfg.weights
    assert cfg.pre_roll_s > 0
    assert 0 <= cfg.iou_threshold <= 1
