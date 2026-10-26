# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from app import __version__
from app.analysis.editorial_contracts import EditorialReviewInputV1, ModelAttempt


def canonical_hash(value: object) -> str:
    """对规范化 JSON 计算身份，不将本机路径或密钥写入指纹资料。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()).hexdigest()


@lru_cache(maxsize=1)
def editorial_code_identity() -> str:
    """读取实际安装源码，跨源码包和 wheel 保持相同的编辑链身份。"""
    names = (
        "analysis/editorial_contracts.py",
        "analysis/editorial_review.py",
        "analysis/editorial_provenance.py",
        "analysis/editorial_review_sources.py",
        "analysis/editorial_evidence.py",
        "analysis/editorial_explanation.py",
        "analysis/event_enricher.py",
        "analysis/model_execution.py",
        "analysis/llm.py",
        "analysis/clip_scorer.py",
        "pipeline/workers/analyze.py",
    )
    return canonical_hash({name: files("app").joinpath(name).read_text(encoding="utf-8") for name in names})


class _PolicyRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    id: str = Field(min_length=1, max_length=256)
    connection: str = Field(max_length=256)
    model: str = Field(min_length=1, max_length=256)
    tokens: int | None = Field(gt=0)
    priority: int


class _Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True, allow_inf_nan=False)
    policy: str = Field(min_length=1, max_length=256)
    prompt: str = Field(pattern=r"^[0-9a-f]{64}$")
    routes: list[_PolicyRoute] = Field(max_length=128)
    attempts: int = Field(ge=1, le=32)
    deadline: float = Field(gt=0)
    request_timeout: float = Field(gt=0)
    max_tokens: int = Field(gt=0)


class ReviewProvenance(BaseModel):
    """执行开始时冻结的策略、原始输入和安装版本；历史缺失不得补造。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True, allow_inf_nan=False)
    schema_version: Literal["editorial-provenance-v1"] = "editorial-provenance-v1"
    app_version: str = Field(min_length=1, max_length=128)
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: dict[str, JsonValue]
    initial_input: EditorialReviewInputV1 | None
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    prior_attempts: list[ModelAttempt] = Field(max_length=32)

    @field_validator("policy")
    @classmethod
    def safe_policy(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """导入历史记录时同样拒绝地址、凭据和任意额外配置。"""
        return _Policy.model_validate(value).model_dump(mode="json")


def capture_provenance(
    policy: dict[str, object],
    initial: EditorialReviewInputV1 | None,
    *,
    operation_id: str,
    prior_attempts: list[ModelAttempt],
) -> ReviewProvenance:
    """只从调用链的白名单策略生成执行来源，不导出整个环境配置。"""
    return ReviewProvenance(
        app_version=__version__,
        code_sha256=editorial_code_identity(),
        policy=policy,
        initial_input=initial,
        operation_id=operation_id,
        prior_attempts=prior_attempts,
    )
