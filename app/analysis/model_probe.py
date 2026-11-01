# ruff: noqa: D100
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

import httpx
from openai import OpenAIError

from app.analysis import llm
from app.analysis.llm_providers import LLMProvider
from app.analysis.model_execution import ModelCallBudget
from app.analysis.model_journal import ModelRequestJournal
from app.core.cancellation import check_cancellation
from app.core.config import settings
from app.models.capacity import ModelCapacityError
from app.models.configuration import ModelAuthorizationError
from app.models.contracts import ModelProbeDiagnostic


def _validate_probe(value: dict[str, object]) -> str:
    if set(value) != {"answer"} or value["answer"] != "pong":
        raise ValueError("模型探测未返回预期字段")
    return "pong"


def diagnose_provider(provider: LLMProvider) -> ModelProbeDiagnostic:
    """在最多两次共享请求内观察文本及结构化样本，不推断最大容量。"""
    budget = ModelCallBudget(
        max_attempts=min(2, settings.editorial_model_max_attempts),
        max_elapsed_s=settings.editorial_model_deadline_s,
        journal=ModelRequestJournal(target=f"probe:{provider.id}"),
    )
    options = llm._low_reasoning_options(provider)
    text_status: Literal["unknown", "accepted", "rejected"] = "unknown"
    structured: Literal["unknown", "accepted", "rejected"] = "unknown"
    reasoning: Literal["not_sent", "unknown", "accepted", "rejected"] = "unknown" if options else "not_sent"
    protocol = cast("Literal['json_schema', 'json_object']", llm._initial_structured_protocol(provider))
    detail = "探测未完成；未验证最大上下文、最大输出或参数执行效果。"
    with budget.operation():
        try:
            llm._complete(provider, "只回复 pong", 4096, options or None, budget=budget, purpose="probe")
            text_status = "accepted"
            reasoning = "accepted" if options else "not_sent"
            detail = "文本样本已通过；结构化协议尚未验证。"
            if budget.interruption() is None:
                spend = llm._today_spend()
                if llm._daily_budget() > 0 and spend >= llm._daily_budget():
                    raise llm.BudgetAccountingError("已达到每日模型预算，停止后续探测")
                if provider.authorize is not None:
                    provider.authorize()
                llm._check_text_budget(budget, before_request=True, force_identity=True)
                schema = {
                    "type": "object",
                    "properties": {"answer": {"type": "string", "enum": ["pong"]}},
                    "required": ["answer"],
                    "additionalProperties": False,
                }
                result = llm._structured_attempt(
                    provider,
                    [
                        {"role": "system", "content": '只返回 JSON 对象 {"answer":"pong"}。'},
                        {"role": "user", "content": "协议探测，不包含用户素材。"},
                    ],
                    schema,
                    4096,
                    budget,
                    "probe",
                    _validate_probe,
                    options or None,
                )
                if result.error is None:
                    structured = "accepted"
                    detail = "文本与结构化样本已通过；服务是否严格执行模式及思考控制仍未知。"
                elif result.error == "unsupported_format":
                    structured = "rejected"
                    detail = "文本样本已通过，服务明确拒绝所请求的结构化协议。"
                else:
                    detail = "文本样本已通过，结构化样本未通过；不能据此认定服务不支持该协议。"
        except ModelAuthorizationError as exc:
            detail = str(exc)
        except (OpenAIError, llm.EmptyLLMResponseError, llm._StructuredFailure, ModelCapacityError, httpx.HTTPError):
            detail = "探测未完成；请检查本次请求诊断。"
            if budget.attempts:
                last = budget.attempts[-1]
                if last.diagnostic is not None:
                    detail = last.diagnostic.summary
                    if options and last.diagnostic.parameter in options and last.diagnostic.http_status in {400, 422}:
                        reasoning = "rejected"
    check_cancellation()
    costs = [attempt.usage.estimated_cost_usd for attempt in budget.attempts]
    cost = (
        sum(value for value in costs if value is not None)
        if costs and all(value is not None for value in costs)
        else None
    )
    return ModelProbeDiagnostic(
        route_id=provider.id,
        connection_id=provider.connection_id or provider.id,
        model=provider.model,
        base_url=provider.base_url,
        observed_at=datetime.now(UTC),
        text=text_status,
        structured=structured,
        structured_protocol=protocol,
        reasoning_parameter=reasoning,
        attempts=len(budget.attempts),
        output_limits=[
            attempt.output_tokens_limit for attempt in budget.attempts if attempt.output_tokens_limit is not None
        ],
        request_ids=[attempt.request_id for attempt in budget.attempts if attempt.request_id is not None],
        elapsed_s=budget.elapsed_s,
        estimated_cost_usd=cost,
        detail=detail,
    )
