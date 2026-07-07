"""数据库实体 — Transcript."""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.db.entities.base import utcnow


class Transcript(SQLModel, table=True):
    """转写结果(``transcripts``): 某片段的语音转文字 + 辅助特征 + ASR 追踪 (V0.1.12.2)。

    V0.1.12: 新增 auxiliary_json 存储 SenseVoice 辅助特征(情感/笑声/音乐/事件)。
    V0.1.12.2: 新增 base_text, final_text, 引擎追踪, 复核记录, 推理耗时。
    """

    __tablename__ = "transcripts"

    id: int | None = Field(default=None, primary_key=True)
    segment_id: int = Field(index=True, description="所属 raw_segments.id")
    language: str | None = Field(default=None, description="识别语言")
    text: str = Field(default="", description="转写全文 (兼容; 等同 final_text)")
    words_json: str | None = Field(default=None, description="词级时间戳 JSON: [{w,start,end}]")
    avg_logprob: float | None = Field(default=None, description="平均置信度")
    auxiliary_json: str | None = Field(default=None, description="V0.1.12: SenseVoice 辅助特征 JSON")

    # V0.1.12.2 新增字段 —— ASR 追踪
    base_text: str | None = Field(default=None, description="主引擎原始文本")
    final_text: str | None = Field(default=None, description="复核后最终文本")
    primary_backend: str | None = Field(default=None, description="主引擎名 (paraformer/whisper/none)")
    primary_model_id: str | None = Field(default=None, description="主模型 ID")
    primary_model_revision: str | None = Field(default=None, description="主模型 revision")
    review_backend: str | None = Field(default=None, description="复核引擎名 (funasr-nano 等)")
    fallback_backend: str | None = Field(default=None, description="兜底引擎名 (whisper)")
    review_triggered: bool = Field(default=False, description="是否触发复核")
    review_risk_score: float | None = Field(default=None, description="最高复核风险评分")
    review_reasons: str | None = Field(default=None, description="复核原因 JSON 列表")
    final_text_source: str | None = Field(
        default=None, description="最终文本来源: primary/review/fallback/manual_review_needed"
    )  # noqa: E501
    inference_duration: float | None = Field(default=None, description="总推理耗时 (秒)")

    created_at: datetime = Field(default_factory=utcnow)

    # V0.1.12.4: 每个片段每种主引擎只有一个正式转录结果 (幂等)
    __table_args__ = {"sqlite_autoincrement": True}
