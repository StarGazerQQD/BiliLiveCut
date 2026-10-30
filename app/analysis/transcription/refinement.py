# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from app.analysis import llm, llm_providers
from app.analysis.editorial_contracts import ModelAttempt
from app.analysis.transcription.content import refined_transcript_text
from app.analysis.transcription.quality import assess_transcript_quality
from app.core import settings_store
from app.core.cancellation import check_cancellation, check_shutdown
from app.db.entities import AppSetting, Transcript


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def refinement_policy() -> str:
    """标识整理提示和实际语义路由，执行额度不影响已完成结果。"""
    return _hash(
        {
            "prompt": llm._TRANSCRIPT_REFINEMENT_PROMPT,
            "routes": [
                [route.id, route.connection_id, route.base_url, route.model, route.priority]
                for route in llm_providers.active_providers("transcript")
            ],
        }
    )


class RefinementDraft(BaseModel):
    """原文独立保存；失败记录可供原任务重新整理，不伪造成功正文。"""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    version: Literal[1] = 1
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["succeeded", "failed", "disabled", "unusable"]
    clean_text: str | None = None
    summary: str | None = None
    operation_id: str | None = None
    attempts: list[ModelAttempt] = Field(default_factory=list, max_length=32)

    @property
    def value(self) -> llm.TranscriptRefinement | None:
        """仅把完整成功结果交给正式正文读取协议。"""
        if self.status != "succeeded":
            return None
        if not self.clean_text or not self.summary:
            raise ValueError("成功整理记录缺少正文或摘要")
        return llm.TranscriptRefinement(clean_text=self.clean_text, summary=self.summary)


def compute_refinement(
    text: str, *, target: str | None = None, cancelled: Callable[[], bool] | None = None
) -> RefinementDraft:
    """在事务外整理，保留实际请求记录，并传播取消和费用记录故障。"""
    check_cancellation()
    policy = refinement_policy()
    identity = _hash(text)
    if not assess_transcript_quality(text).usable:
        return RefinementDraft(source_sha256=identity, policy_sha256=policy, status="unusable")
    if not settings_store.transcript_llm_refine_enabled() or not llm_providers.active_providers("transcript"):
        return RefinementDraft(source_sha256=identity, policy_sha256=policy, status="disabled")
    budget = llm.new_editorial_budget(target=target, cancelled=cancelled)
    result = llm.refine_transcript(text, budget=budget)
    check_cancellation()
    return RefinementDraft(
        source_sha256=identity,
        policy_sha256=policy,
        status="succeeded" if result is not None else "failed",
        clean_text=result.clean_text if result is not None else None,
        summary=result.summary if result is not None else None,
        operation_id=budget.operation_id,
        attempts=list(budget.attempts),
    )


def transcript_refinement_revision(transcript: Transcript) -> str:
    """核对已有原文、词时间和人工状态，禁止迟到结果覆盖期间的修改。"""
    return _hash(transcript.model_dump(mode="json"))


def store_refinement(db: Session, transcript: Transcript, draft: RefinementDraft) -> bool:
    """复验本次执行快照后同事务保存；不写回 ASR 原文和词时间。"""
    check_shutdown()
    if transcript.final_text_source == "manual" or _hash(transcript.final_text) != draft.source_sha256:
        return False
    if refinement_policy() != draft.policy_sha256:
        return False
    value = draft.value
    if value is not None:
        auxiliary = json.loads(transcript.auxiliary_json or "{}")
        auxiliary["transcript_refinement"] = {
            "version": 1,
            "applied": True,
            "clean_text": value.clean_text,
            "summary": value.summary,
        }
        transcript.auxiliary_json = json.dumps(auxiliary, ensure_ascii=False, allow_nan=False)
        db.add(transcript)
    key = f"transcript_refinement:v1:{transcript.segment_id}"
    row = db.get(AppSetting, key) or AppSetting(key=key, value="{}")
    row.value = draft.model_dump_json()
    db.add(row)
    return True


def refinement_needs_retry(db: Session, transcript: Transcript) -> bool:
    """场次重新分析时仅补做已记录失败且仍对应当前原文的自动整理。"""
    if transcript.final_text_source == "manual" or refined_transcript_text(transcript) is not None:
        return False
    row = db.get(AppSetting, f"transcript_refinement:v1:{transcript.segment_id}")
    if row is None:
        return False
    draft = RefinementDraft.model_validate_json(row.value)
    return (
        draft.status == "failed"
        and draft.source_sha256 == _hash(transcript.final_text)
        and settings_store.transcript_llm_refine_enabled()
        and bool(llm_providers.active_providers("transcript"))
    )
