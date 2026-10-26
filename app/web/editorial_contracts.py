# ruff: noqa: D100
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.analysis.editorial_contracts import ContextDirection, EditorialEvidence, EditorialExecutionV1, FailureCode
from app.analysis.reanalysis_targets import ReanalysisSelection, ReanalysisTarget


class EditorialPresentation(BaseModel):
    """时间线与审片共用的已验证展示，不携带密钥、路径或原始模型响应。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal["editorial-view-v1"] = "editorial-view-v1"
    hotspot_event_id: int | None = None
    candidate_id: int | None = None
    event_id: int | None = None
    title: str
    summary: str
    explanation_state: Literal["available", "failed", "unverified", "not_run"]
    explanation_error: FailureCode | None = None
    editorial_state: Literal["succeeded", "failed", "disabled", "cancelled", "not_run", "invalid"] = "not_run"
    execution: EditorialExecutionV1 | None = None
    evidence: list[EditorialEvidence] = Field(default_factory=list)
    time_origin_epoch_s: float | None = None
    expanded_context: ContextDirection | None = None
    technical_score: float | None = None


class EditorialTimelinePoint(BaseModel):
    """为新增字段提供正式类型，同时保留已有时间线兼容字段。"""

    model_config = ConfigDict(extra="allow")
    key: str
    point_type: Literal["hotspot", "segment"]
    hotspot_event_id: int | None = None
    candidate_id: int | None = None
    event_id: int | None = None
    title: str
    summary: str
    offset_s: float
    duration_s: float
    hotspot_duration_s: float | None = None
    clip_duration_s: float | None = None
    rejected: bool
    editorial: EditorialPresentation


class EditorialTimelineResponse(BaseModel):
    """场次读取只投影持久状态，不能隐式发起模型请求。"""

    session: dict[str, JsonValue]
    timezone: str
    points: list[EditorialTimelinePoint]
    counts: dict[str, int]
    whole_session_summary: dict[str, JsonValue] | None = None


class EditorialPreview(BaseModel):
    """服务端生成的有限目标快照。"""

    session_id: int
    total: int
    targets: list[ReanalysisTarget]
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    next_offset: int | None


class EditorialSubmission(BaseModel):
    """明确提交与预览完全一致的选择。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    selection: ReanalysisSelection
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class EditorialJobAccepted(BaseModel):
    """后续通过既有作业 HTTP 接口恢复进度和取消操作。"""

    job_id: str
    status: str
    status_url: str
