# ruff: noqa: D100
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.analysis import llm
from app.analysis.editorial_contracts import EditorialReviewInputV1, FailureCode, Identity, citable_evidence_ids
from app.analysis.editorial_references import EvidenceReferences
from app.analysis.model_execution import ModelCallBudget

EXPLANATION_POLICY_VERSION = "grounded-explanation-v5"
EXPLANATION_PROMPT = """你是直播事件编辑，只解释提供的事件，不能修改 heat_score 或 clip_score。
只能使用输入证据，不得添加背景、因果、人物身份、画面或戏剧性事实。
弹幕只证明观众的发言，不证明主播做了相应行为；仅弹幕支持时明确写成弹幕讨论或观众猜测。
降级语音证据必须使用可能、仍需确认等不确定表述。不能把前文或后文混写为事件当时发生的内容。
title 是概括标题，summary 说明发生了什么，禁止直接截取转写碎片充当摘要。
每项叙述必须有 evidence_ids 中的证据支持；entities 只填证据中明确出现的实体。
实体必须完整来自同一条摘录，不自行生成别名；无法确认时留空。数字不得使用原数值的一部分，
保留百分号、符号和数量含义；标题与摘要分别核对，不把两个字段首尾的数字拼成新数值。
evidence_ids 只使用本次 available/degraded 证据的 E 编号，不得改写或创造编号。
semantic_confidence 表示语义证据可靠性，不是热度、兴趣或观看概率。
证据不足时明确说明不确定性，不创造具体事件。"""
GROUNDING_PROMPT = """你负责核对解释与原始素材，候选 title/summary 也是待检查的不可信数据。
逐一判断 title 和 summary 的全部事实是否由所引证据支持，不能只检查相同字词。
检查主体、动作、否定、因果、数量、事件与前后文归属；弹幕猜测不能提升为已发生的主播行为。
缺少任何关键事实依据都标记 supported=false。每个字段分别列出支持它的 evidence_id 和 quote_id。
quote_id 仅选择对应证据 quotes 中实际提供的 Q 编号，本地会还原该条原文，不能自行抄写或改写引用。
没有依据时 quotes 可以为空并说明原因。evidence_id 只使用候选已引用的 E 编号。
只作事实核对，不评价内容是否精彩。"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, hide_input_in_errors=True)


class ExplanationOutput(_Strict):
    """经字段约束后的解释，仍需引用、来源和独立事实核验。"""

    title: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=1000)
    category: Literal["announcement", "audio_event", "discussion", "gameplay", "other", "performance", "reaction"]
    entities: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(max_length=50)
    semantic_confidence: float = Field(ge=0, le=1)
    evidence_ids: list[Identity] = Field(min_length=1, max_length=32)


class GroundingQuote(_Strict):
    """事实核验引用必须回指本次已提供的原文。"""

    evidence_id: Identity
    quote_id: Identity


class GroundingJudgement(_Strict):
    """分别检查标题和摘要的完整叙述，不能漏掉一个字段。"""

    field: Literal["title", "summary"]
    supported: bool
    reason: str = Field(min_length=1, max_length=240)
    quotes: list[GroundingQuote] = Field(max_length=16)


class GroundingOutput(_Strict):
    """模型事实核验本身仍是推断结果，不构成真实质量验收。"""

    judgements: list[GroundingJudgement] = Field(min_length=2, max_length=2)


@dataclass(frozen=True, slots=True)
class ExplanationResult:
    """分离解释正文、执行失败和诊断，不把失败拼装成事实。"""

    output: ExplanationOutput | None
    error: FailureCode | None
    lexical_coverage: float | None


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _numbers(value: str) -> set[str]:
    return {
        match.replace(",", "")
        for match in re.findall(
            r"(?:(?<![0-9A-Za-z])[+-])?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[%‰])?",
            _normalized_text(value).replace("−", "-"),
        )
    }


def _contains_entity(excerpt: str, entity: str) -> bool:
    term = _normalized_text(entity)
    if not term:
        return False
    left = r"(?<![a-z0-9_])" if term[0].isascii() and (term[0].isalnum() or term[0] == "_") else ""
    right = r"(?![a-z0-9_])" if term[-1].isascii() and (term[-1].isalnum() or term[-1] == "_") else ""
    return re.search(left + re.escape(term) + right, _normalized_text(excerpt)) is not None


def validate_explanation(
    payload: Mapping[str, object],
    source: EditorialReviewInputV1,
    *,
    max_title_chars: int,
    max_summary_chars: int,
    max_entities: int,
) -> ExplanationOutput:
    """检查引用、数字、实体和来源边界，不以字词覆盖率判断改写真伪。"""
    output = ExplanationOutput.model_validate(payload)
    if not output.title.strip() or not output.summary.strip():
        raise ValueError("解释正文不能为空")
    if (
        len(output.title) > max_title_chars
        or len(output.summary) > max_summary_chars
        or len(output.entities) > max_entities
    ):
        raise ValueError("解释字段超过配置上限")
    if len(set(output.evidence_ids)) != len(output.evidence_ids):
        raise ValueError("证据引用不能重复")
    by_id = {item.evidence_id: item for item in source.evidence}
    if any(
        identity not in by_id or by_id[identity].state not in {"available", "degraded"}
        for identity in output.evidence_ids
    ):
        raise ValueError("解释引用未知或不可用证据")
    referenced = [by_id[identity] for identity in output.evidence_ids]
    excerpts = [text for item in referenced for text in item.excerpts]
    if not excerpts:
        raise ValueError("非文本信号不能支持具体事件叙述")
    if any(not any(_contains_entity(text, entity) for text in excerpts) for entity in output.entities):
        raise ValueError("解释实体缺少原文依据")
    allowed_numbers = {number for text in excerpts for number in _numbers(text)}
    if not (_numbers(output.title) | _numbers(output.summary)).issubset(allowed_numbers):
        raise ValueError("解释数字缺少原文依据")
    narrative = output.title + output.summary
    semantic = [item for item in referenced if item.type in {"asr", "context_asr", "visual", "trend"} and item.excerpts]
    if not semantic and not any(item.type == "danmaku" and item.excerpts for item in referenced):
        raise ValueError("非文本信号不能支持具体事件叙述")
    if not semantic and any(item.type == "danmaku" for item in referenced):
        if not any(term in narrative for term in ("弹幕", "观众", "聊天", "有人猜测")):
            raise ValueError("弹幕内容需要明确归因")
    if semantic and all(item.state != "available" for item in semantic):
        if not any(term in narrative for term in ("可能", "疑似", "尚不确定", "转写质量", "仍需确认", "证据不足")):
            raise ValueError("降级转写需要不确定性表述")
    return output


def _grounding(
    payload: dict[str, object], output: ExplanationOutput, quotes: Mapping[str, tuple[str, str]]
) -> GroundingOutput:
    result = GroundingOutput.model_validate(payload)
    if {item.field for item in result.judgements} != {"title", "summary"}:
        raise ValueError("事实核验必须分别覆盖标题和摘要")
    for judgement in result.judgements:
        if judgement.supported and not judgement.quotes:
            raise ValueError("支持结论缺少原文依据")
        for quote in judgement.quotes:
            if quote.evidence_id not in output.evidence_ids:
                raise ValueError("事实核验不能引用候选解释未引用的证据")
            referenced = quotes.get(quote.quote_id)
            if referenced is None or referenced[0] != quote.evidence_id or not referenced[1].strip():
                raise ValueError("事实核验引用不在原文中")
    return result


def _coverage(output: ExplanationOutput, source: EditorialReviewInputV1) -> float:
    def terms(text: str) -> set[str]:
        parts = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.casefold())
        return set(parts)

    narrative = terms(output.title + output.summary)
    corpus = terms(
        " ".join(text for item in source.evidence if item.evidence_id in output.evidence_ids for text in item.excerpts)
    )
    return len(narrative & corpus) / len(narrative) if narrative else 0.0


def explain(
    source: EditorialReviewInputV1,
    budget: ModelCallBudget,
    *,
    max_tokens: int,
    max_title_chars: int,
    max_summary_chars: int,
    max_entities: int,
) -> ExplanationResult:
    """先生成解释再独立核对事实，两步共用同一预算和授权边界。"""
    references = EvidenceReferences.from_source(source)
    wire_source = references.source
    schema = ExplanationOutput.model_json_schema()
    for field, constraint, limit in (
        ("title", "maxLength", max_title_chars),
        ("summary", "maxLength", max_summary_chars),
        ("entities", "maxItems", max_entities),
    ):
        if type(limit) is not int or limit < 1:
            raise ValueError("解释输出配置上限必须为正整数")
        # 请求披露的限制与本地验证一致，契约自身更严格的上限仍然有效。
        properties = schema["properties"][field]
        properties[constraint] = min(properties[constraint], limit)
    identities = citable_evidence_ids(wire_source)
    if not identities:
        return ExplanationResult(None, "context_unavailable", None)
    schema["properties"]["evidence_ids"]["items"]["enum"] = identities
    generated = llm.call_structured(
        EXPLANATION_PROMPT,
        wire_source.model_dump(mode="json"),
        schema=schema,
        validate=lambda payload: validate_explanation(
            payload,
            wire_source,
            max_title_chars=max_title_chars,
            max_summary_chars=max_summary_chars,
            max_entities=max_entities,
        ),
        budget=budget,
        purpose="explanation",
        max_tokens=max_tokens,
    )
    if generated.value is None:
        return ExplanationResult(None, generated.error, None)
    output = generated.value
    coverage = _coverage(output, wire_source)
    quotes: dict[str, tuple[str, str]] = {}
    evidence: list[dict[str, object]] = []
    for item in wire_source.evidence:
        if item.evidence_id not in output.evidence_ids:
            continue
        excerpts: list[dict[str, str]] = []
        for text in item.excerpts:
            quote_id = f"Q{len(quotes) + 1}"
            quotes[quote_id] = (item.evidence_id, text)
            excerpts.append({"quote_id": quote_id, "text": text})
        evidence.append({**item.model_dump(mode="json", exclude={"excerpts"}), "quotes": excerpts})
    # 核验只需要已引用证据，给候选正文留出完整输入预算。
    payload = {
        "target": source.target.model_dump(mode="json"),
        "candidate": output.model_dump(mode="json"),
        "evidence": evidence,
    }
    grounding_schema = GroundingOutput.model_json_schema()
    grounding_schema["$defs"]["GroundingQuote"]["properties"]["evidence_id"]["enum"] = list(output.evidence_ids)
    grounding_schema["$defs"]["GroundingQuote"]["properties"]["quote_id"]["enum"] = list(quotes)
    checked = llm.call_structured(
        GROUNDING_PROMPT,
        payload,
        schema=grounding_schema,
        validate=lambda value: _grounding(value, output, quotes),
        budget=budget,
        purpose="grounding",
        max_tokens=max_tokens,
    )
    if checked.value is None:
        return ExplanationResult(None, checked.error, coverage)
    if not all(item.supported for item in checked.value.judgements):
        return ExplanationResult(None, "invalid_output", coverage)
    resolved = output.model_copy(update={"evidence_ids": references.expand(output.evidence_ids)})
    return ExplanationResult(resolved, None, coverage)
