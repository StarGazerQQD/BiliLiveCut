# ruff: noqa: D100
from __future__ import annotations

import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

INPUT_MAX_BYTES = 65_536
OUTPUT_MAX_BYTES = 16_384
REVIEW_SCHEMA_VERSION = "editorial-review-v1"
Identity = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f\x7f]+$")]
ShortText = Annotated[str, Field(min_length=1, max_length=240, pattern=r"\S")]
Seconds = Annotated[float, Field(ge=0)]
EvidenceState = Literal["available", "degraded", "missing", "disabled", "error", "pending"]
EvidenceType = Literal["asr", "context_asr", "danmaku", "audio", "sensevoice", "trend", "visual"]
ContextDirection = Literal["before", "after", "both"]
FailureCode = Literal[
    "disabled",
    "daily_budget",
    "attempt_budget",
    "deadline",
    "timeout",
    "rate_limited",
    "unauthorized",
    "provider_error",
    "unsupported_format",
    "empty_response",
    "invalid_output",
    "context_unavailable",
    "cancelled",
    "evidence_changed",
]


class _StrictContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        frozen=True,
        revalidate_instances="always",
    )


class TimeRange(_StrictContract):
    """相对场次开始的有限秒数范围，不能表示未知时间。"""

    start_s: Seconds
    end_s: Seconds

    @model_validator(mode="after")
    def ordered(self) -> Self:
        """拒绝空窗口和倒置范围。"""
        if self.end_s <= self.start_s:
            raise ValueError("时间范围终点必须晚于起点")
        return self

    def contains(self, other: TimeRange) -> bool:
        """判断另一范围是否完全位于本范围内。"""
        return self.start_s <= other.start_s and other.end_s <= self.end_s


class EditorialTarget(_StrictContract):
    """事件路径和旧片段路径共用的稳定分析对象。"""

    kind: Literal["hotspot", "segment"]
    session_id: int = Field(ge=1)
    target_id: int = Field(ge=1)
    target_key: Identity
    bounds: TimeRange
    peak_s: Seconds

    @model_validator(mode="after")
    def peak_within_event(self) -> Self:
        """峰值必须位于事件自身范围，保留场次相对时间语义。"""
        if not self.bounds.start_s <= self.peak_s <= self.bounds.end_s:
            raise ValueError("事件峰值超出自身时间范围")
        return self


class EvidenceDetail(_StrictContract):
    """允许进入模型输入的有限诊断字段，不接收路径、凭据或任意元数据。"""

    segment_id: int | None = Field(default=None, ge=1)
    backend: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)
    quality: ShortText | None = None
    count: int | None = Field(default=None, ge=0)
    role: str | None = Field(default=None, max_length=64)
    metrics: dict[Annotated[str, Field(min_length=1, max_length=64)], float] = Field(
        default_factory=dict,
        max_length=24,
    )


class EditorialEvidence(_StrictContract):
    """明确记录来源和时间精度的一条不可信素材证据。"""

    evidence_id: Identity
    type: EvidenceType
    state: EvidenceState
    start_s: Seconds | None
    end_s: Seconds | None
    timing_precision: Literal["word", "segment", "event", "unknown"]
    context_role: Literal["before", "event", "after", "mixed", "unknown"]
    excerpts: list[Annotated[str, Field(min_length=1, max_length=2_000, pattern=r"\S")]] = Field(max_length=8)
    detail: EvidenceDetail = Field(default_factory=EvidenceDetail)

    @model_validator(mode="after")
    def validate_time_and_state(self) -> Self:
        """未知时间不填写假零值，不可用证据不携带可引用正文。"""
        if self.timing_precision == "unknown":
            if self.start_s is not None or self.end_s is not None or self.context_role != "unknown":
                raise ValueError("未知时间证据必须保留空时间与未知上下文位置")
        elif self.start_s is None or self.end_s is None or self.end_s < self.start_s:
            raise ValueError("已知时间证据需要有效的起止时间")
        if self.state not in {"available", "degraded"} and self.excerpts:
            raise ValueError("不可用证据不能携带可引用正文")
        if self.state in {"available", "degraded"} and not self.excerpts and not self.detail.metrics:
            raise ValueError("可用证据必须包含实际正文或测量指标")
        return self


class EditorialReviewInputV1(_StrictContract):
    """完整且有大小上限的编辑输入，范围由实际录制来源确定。"""

    schema_version: Literal["editorial-input-v1"] = "editorial-input-v1"
    time_base: Literal["session_seconds"] = "session_seconds"
    target: EditorialTarget
    window: TimeRange
    recording_ranges: list[TimeRange] = Field(min_length=1, max_length=128)
    truncated: bool
    audience_profile: str = Field(min_length=1, max_length=1_000, pattern=r"\S")
    preference_version: Identity
    evidence: list[EditorialEvidence] = Field(max_length=128)

    @model_validator(mode="after")
    def validate_ranges_and_size(self) -> Self:
        """核对连续录制、证据身份和完整 JSON 大小。"""
        ranges = self.recording_ranges
        if any(left.end_s >= right.start_s for left, right in zip(ranges, ranges[1:], strict=False)):
            raise ValueError("录制范围必须按时间排序并合并相邻或重叠范围")
        if not any(block.contains(self.window) for block in ranges):
            raise ValueError("输入窗口必须处于一个真实连续录制范围内")
        if not self.window.start_s <= self.target.peak_s <= self.window.end_s:
            raise ValueError("输入窗口必须覆盖事件峰值")
        if not self.truncated and not self.window.contains(self.target.bounds):
            raise ValueError("未覆盖完整事件的输入必须明确标记截断")
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("证据 ID 不能重复")
        for item in self.evidence:
            if item.start_s is not None and item.end_s is not None:
                if item.start_s < self.window.start_s or item.end_s > self.window.end_s:
                    raise ValueError("证据时间超出实际输入窗口")
                if item.context_role == "before" and item.end_s > self.target.bounds.start_s:
                    raise ValueError("前文证据与事件范围矛盾")
                if item.context_role == "after" and item.start_s < self.target.bounds.end_s:
                    raise ValueError("后文证据与事件范围矛盾")
                if item.context_role == "event" and (
                    item.start_s < self.target.bounds.start_s or item.end_s > self.target.bounds.end_s
                ):
                    raise ValueError("事件证据超出事件自身范围")
        if len(self.model_dump_json().encode("utf-8")) > INPUT_MAX_BYTES:
            raise ValueError("完整编辑输入超过字节上限")
        return self


class SuggestedBounds(TimeRange):
    """由明确时间证据支持的成片范围建议。"""

    evidence_ids: list[Identity] = Field(min_length=1, max_length=32)


class EditorialReviewResultV1(_StrictContract):
    """模型的编辑判断；技术调用失败不构造这个业务对象。"""

    schema_version: Literal["editorial-review-v1"]
    verdict: Literal["recommend", "reject", "needs_review"]
    interest_rating: int | None = Field(ge=0, le=4)
    hook: str = Field(max_length=120)
    reason: ShortText
    evidence_ids: list[Identity] = Field(max_length=32)
    standalone: bool | None
    boundary_status: Literal["complete", "needs_expansion", "uncertain"]
    suggested_bounds: SuggestedBounds | None
    missing_context: ContextDirection | None
    uncertainties: list[ShortText] = Field(max_length=12)

    @model_validator(mode="after")
    def coherent_decision(self) -> Self:
        """拒绝缺少依据或相互矛盾的确定性判断。"""
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("判断引用不能重复")
        if self.verdict in {"recommend", "reject"} and not self.evidence_ids:
            raise ValueError("推荐与拒绝必须提供证据引用")
        if self.verdict == "recommend" and (self.interest_rating is None or not self.hook.strip()):
            raise ValueError("推荐必须说明吸引点和兴趣等级")
        if self.verdict == "needs_review" and not self.uncertainties:
            raise ValueError("待复核必须明确不确定因素")
        if (self.boundary_status == "needs_expansion") != (self.missing_context is not None):
            raise ValueError("需要扩展时必须明确缺少前文、后文或两者")
        if self.boundary_status == "complete" and self.standalone is not True:
            raise ValueError("完整边界必须已有独立可理解的判断依据")
        if self.suggested_bounds is not None:
            ids = self.suggested_bounds.evidence_ids
            if len(set(ids)) != len(ids) or not set(ids).issubset(self.evidence_ids):
                raise ValueError("边界证据必须是无重复的判断证据子集")
        return self


class TokenUsage(_StrictContract):
    """服务实际报告的用量；缺失值保留为空，价格为配置估算。"""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def cost_requires_usage(self) -> Self:
        """没有实际输入输出用量时不得伪造零成本。"""
        if self.estimated_cost_usd is not None and (self.input_tokens is None or self.output_tokens is None):
            raise ValueError("成本估算需要实际输入及输出用量")
        return self


class ModelAttempt(_StrictContract):
    """单次实际请求的诊断，不保存地址、凭据、原始正文或异常。"""

    number: int = Field(ge=1, le=32)
    purpose: Literal["explanation", "review", "grounding", "repair", "context"]
    provider_id: Identity
    model: str = Field(min_length=1, max_length=256)
    protocol: Literal["json_schema", "json_object", "text"]
    status: Literal["success", "failed"]
    error: FailureCode | None
    elapsed_s: Seconds
    usage: TokenUsage = Field(default_factory=TokenUsage)

    @model_validator(mode="after")
    def error_matches_status(self) -> Self:
        """成功请求和失败请求具有互斥的错误状态。"""
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("请求状态与错误分类不一致")
        return self


class EditorialExecutionV1(_StrictContract):
    """可持久化的执行结果，与编辑结论和人工复核状态分别保存。"""

    schema_version: Literal["editorial-execution-v1"] = "editorial-execution-v1"
    status: Literal["succeeded", "failed", "disabled", "cancelled"]
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: Identity
    result: EditorialReviewResultV1 | None
    error: FailureCode | None
    attempts: list[ModelAttempt] = Field(max_length=32)
    elapsed_s: Seconds

    @model_validator(mode="after")
    def coherent_execution(self) -> Self:
        """禁止把错误、关闭和取消包装成成功的业务结论。"""
        if self.status == "succeeded":
            if self.result is None or self.error is not None:
                raise ValueError("成功执行必须包含判断且没有执行错误")
            if not self.attempts or self.attempts[-1].status != "success":
                raise ValueError("成功执行必须有实际成功请求")
        elif self.result is not None or self.error is None:
            raise ValueError("未成功执行不能包含编辑判断，必须保留错误分类")
        if self.status == "disabled" and (self.error != "disabled" or self.attempts):
            raise ValueError("关闭状态不能发起实际请求")
        if self.status == "cancelled" and self.error != "cancelled":
            raise ValueError("取消状态必须保留取消分类")
        if self.error in {"disabled", "cancelled"} and self.status != self.error:
            raise ValueError("关闭和取消分类必须对应各自执行状态")
        if [attempt.number for attempt in self.attempts] != list(range(1, len(self.attempts) + 1)):
            raise ValueError("请求序号必须从 1 连续记录")
        if sum(attempt.elapsed_s for attempt in self.attempts) > self.elapsed_s + 0.001:
            raise ValueError("总耗时不能少于串行请求耗时")
        return self


def validate_review_evidence(
    result: EditorialReviewResultV1,
    source: EditorialReviewInputV1,
) -> EditorialReviewResultV1:
    """对照原始输入核验引用和建议范围，不将结构正确等同于事实正确。"""
    result = EditorialReviewResultV1.model_validate(result)
    source = EditorialReviewInputV1.model_validate(source)
    evidence = {item.evidence_id: item for item in source.evidence}
    for identity in result.evidence_ids:
        item = evidence.get(identity)
        if item is None or item.state not in {"available", "degraded"}:
            raise ValueError("判断引用了不存在或不可用的证据")
    if result.verdict in {"recommend", "reject"} and not any(
        evidence[identity].state == "available" for identity in result.evidence_ids
    ):
        raise ValueError("仅有降级证据时需要人工复核")
    suggestion = result.suggested_bounds
    if suggestion is not None:
        if not source.window.contains(suggestion) or not any(
            block.contains(suggestion) for block in source.recording_ranges
        ):
            raise ValueError("建议范围超出已提供上下文或跨越录制缺口")
        if suggestion.end_s <= source.target.bounds.start_s or suggestion.start_s >= source.target.bounds.end_s:
            raise ValueError("建议范围没有覆盖目标事件")
        for identity in suggestion.evidence_ids:
            item = evidence[identity]
            if item.timing_precision == "unknown" or item.start_s is None or item.end_s is None:
                raise ValueError("没有时间定位的证据不能支持边界建议")
            if item.end_s < suggestion.start_s or item.start_s > suggestion.end_s:
                raise ValueError("边界证据与建议范围没有交集")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 不允许重复字段")
        result[key] = value
    return result


def parse_editorial_review(text: str, source: EditorialReviewInputV1) -> EditorialReviewResultV1:
    """使用标准 JSON 解析器拒绝围栏、重复字段、超大输出及非法引用。"""
    if len(text.encode("utf-8")) > OUTPUT_MAX_BYTES:
        raise ValueError("编辑判断输出超过字节上限")
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("编辑判断必须是单个有效 JSON 对象") from exc
    return validate_review_evidence(EditorialReviewResultV1.model_validate(payload), source)
