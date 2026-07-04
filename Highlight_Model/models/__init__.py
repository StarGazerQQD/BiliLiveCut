"""模型模块。

提供训练入口、评估脚本、自学习引擎和推理接口。
"""
from __future__ import annotations

from Highlight_Model.models.inference import ModelInference
from Highlight_Model.models.self_learn import SelfLearnEngine, SelfLearnResult

__all__ = ["ModelInference", "SelfLearnEngine", "SelfLearnResult"]
