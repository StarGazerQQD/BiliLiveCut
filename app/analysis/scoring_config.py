"""高光评分配置加载。

从 ``config/scoring.yaml`` 读取权重、融合系数、上下文留白与去重参数,
并提供合理默认值(配置缺失时仍可运行)。配置在进程内缓存一次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from loguru import logger

# 配置文件相对工程根目录。
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "scoring.yaml"

_DEFAULT_WEIGHTS: dict[str, float] = {
    "volume": 0.25,
    "danmaku": 0.30,
    "keywords": 0.20,
    "speech_rate": 0.15,
    "laughter": 0.10,
    "trend": 0.15,  # 网感资料库:片段题材与近期热门内容的关联度
}


@dataclass(slots=True)
class ScoringConfig:
    """评分配置。

    :param weights: 各特征维度权重。
    :param alpha: 规则分在综合分中的系数。
    :param beta: LLM 分在综合分中的系数。
    :param pre_roll_s: 爆点前留白(秒)。
    :param post_roll_s: 爆点后留白(秒)。
    :param iou_threshold: 候选区间合并的 IoU 阈值。
    :param cooldown_s: 同类爆点冷却时间(秒)。
    """

    weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    alpha: float = 0.5
    beta: float = 0.5
    pre_roll_s: float = 15.0
    post_roll_s: float = 30.0
    iou_threshold: float = 0.5
    cooldown_s: float = 60.0


@lru_cache(maxsize=1)
def get_scoring_config() -> ScoringConfig:
    """加载并缓存评分配置。

    :returns: :class:`ScoringConfig`;文件缺失或解析失败时返回默认配置。
    """
    if not _CONFIG_PATH.exists():
        logger.warning("未找到评分配置 {},使用默认值。", _CONFIG_PATH)
        return ScoringConfig()

    try:
        data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.error("解析评分配置失败,使用默认值: {}", exc)
        return ScoringConfig()

    weights = {**_DEFAULT_WEIGHTS, **(data.get("weights") or {})}
    fusion = data.get("fusion") or {}
    context = data.get("context") or {}
    dedup = data.get("dedup") or {}

    return ScoringConfig(
        weights={k: float(v) for k, v in weights.items()},
        alpha=float(fusion.get("alpha", 0.5)),
        beta=float(fusion.get("beta", 0.5)),
        pre_roll_s=float(context.get("pre_roll_s", 15)),
        post_roll_s=float(context.get("post_roll_s", 30)),
        iou_threshold=float(dedup.get("iou_threshold", 0.5)),
        cooldown_s=float(dedup.get("cooldown_s", 60)),
    )
