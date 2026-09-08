"""高光评分配置加载。

从 ``config/scoring.yaml`` 读取权重、融合系数、上下文留白与去重参数,
并提供合理默认值(配置缺失时仍可运行)。文件基线缓存一次，数据库覆盖按任务快照读取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

# 配置文件相对工程根目录。
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "scoring.yaml"

_DEFAULT_WEIGHTS: dict[str, float] = {
    # 权重和 > 1.0 是预期行为:weighted_rule_score() 会对参与维度自动归一化,
    # 因此这里反映的是各维度的**相对重要性**而非百分比。
    "volume": 0.25,
    "danmaku": 0.30,
    "keywords": 0.20,
    "speech_rate": 0.15,
    "laughter": 0.10,
    "trend": 0.15,  # 网感资料库:片段题材与近期热门内容的关联度
    "danmaku_sentiment": 0.15,  # 弹幕情绪:重复率/感叹号密度/特定梗
    "audio_events": 0.10,  # V0.1.12.2: 音频事件(笑声/掌声/惊讶) SenseVoice 辅助
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
    pre_roll_s: float = 60.0
    post_roll_s: float = 30.0
    iou_threshold: float = 0.5
    cooldown_s: float = 60.0


@lru_cache(maxsize=1)
def get_scoring_baseline() -> ScoringConfig:
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
        pre_roll_s=float(context.get("pre_roll_s", 60)),
        post_roll_s=float(context.get("post_roll_s", 30)),
        iou_threshold=float(dedup.get("iou_threshold", 0.5)),
        cooldown_s=float(dedup.get("cooldown_s", 60)),
    )


class ScoringOptions(BaseModel):
    """完整评分参数；拒绝未知维度、非有限数和无效权重。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    weights: dict[str, float] = Field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    alpha: float = Field(default=0.5, ge=0, le=1)
    beta: float = Field(default=0.5, ge=0, le=1)
    pre_roll_s: float = Field(default=60, ge=0, le=600)
    post_roll_s: float = Field(default=30, ge=0, le=600)
    iou_threshold: float = Field(default=0.5, ge=0, le=1)
    cooldown_s: float = Field(default=60, ge=0, le=3600)

    @model_validator(mode="after")
    def validate_weights(self) -> ScoringOptions:
        """固定八个评分维度，至少一个维度及一个融合来源必须有效。"""
        if set(self.weights) != set(_DEFAULT_WEIGHTS):
            raise ValueError("评分维度必须与注册的八个维度一致")
        if any(weight < 0 for weight in self.weights.values()) or sum(self.weights.values()) <= 0:
            raise ValueError("评分权重必须非负且总和大于 0")
        if self.alpha + self.beta <= 0:
            raise ValueError("规则分与 LLM 分融合系数不能同时为 0")
        return self


def scoring_defaults() -> ScoringOptions:
    """把项目 YAML 作为业务默认值；恢复覆盖会重新使用该默认值。"""
    from dataclasses import asdict

    return ScoringOptions.model_validate(asdict(get_scoring_baseline()))


def get_scoring_config() -> ScoringConfig:
    """从任务快照读取评分覆盖，未配置时保持项目 YAML 默认。"""
    from app.core.settings_store import get_setting

    raw = get_setting("scoring_configuration", "")
    options = ScoringOptions.model_validate_json(raw) if raw else scoring_defaults()
    return ScoringConfig(**options.model_dump())


get_scoring_config.cache_clear = get_scoring_baseline.cache_clear
