# ruff: noqa: D100
from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING, Generic, TypeVar
from uuid import uuid4

from app.analysis.editorial_contracts import FailureCode, ModelAttempt, ModelProtocol, ModelPurpose, TokenUsage
from app.analysis.search_contracts import SearchEvidence
from app.core.cancellation import CancellationProbe, cancellation_requested

T = TypeVar("T")

if TYPE_CHECKING:
    from app.analysis.model_journal import ModelRequestJournal


@dataclass(frozen=True, slots=True)
class StructuredCallResult(Generic[T]):
    """只携带通过业务验证的值，失败时保留分类而不构造业务结论。"""

    value: T | None = field(repr=False)
    error: FailureCode | None
    retry_after_s: float | None = None

    def __post_init__(self) -> None:
        """拒绝同时包含成功值与失败分类的矛盾状态。"""
        if (self.value is None) == (self.error is None):
            raise ValueError("调用结果必须且只能包含有效值或错误分类")


class ModelCallBudget:
    """一次编辑操作共享的串行请求预算，解释、复核与重算不得重置它。"""

    def __init__(
        self,
        *,
        max_attempts: int,
        max_elapsed_s: float,
        cancelled: Callable[[], bool] | None = None,
        journal: ModelRequestJournal | None = None,
        operation_id: str | None = None,
    ) -> None:
        if type(max_attempts) is not int or not 1 <= max_attempts <= 32:
            raise ValueError("模型总请求次数必须在 1~32 之间")
        if isinstance(max_elapsed_s, bool) or not math.isfinite(max_elapsed_s) or max_elapsed_s <= 0:
            raise ValueError("模型总时间预算必须为有限正数")
        self._max_attempts = max_attempts
        self._operation_id = uuid4().hex
        self._started = time.monotonic()
        self._deadline = self._started + max_elapsed_s
        self._cancelled = CancellationProbe(cancelled) if cancelled is not None else None
        self._attempts: list[ModelAttempt] = []
        self._search_results: list[SearchEvidence] = []
        self._repair_used = False
        self._context_used = False
        self._empty_used = False
        self._operation = Lock()
        self._protocols: dict[tuple[str, str], ModelProtocol] = {}
        self._transport_retries: set[tuple[str, str, str]] = set()
        self._journal = journal
        self._prior_requests = 0
        self._last_failure: FailureCode | None = None
        if operation_id is not None:
            if journal is None:
                raise ValueError("持久模型操作必须有请求记录器")
            state = journal.open_operation(operation_id, max_attempts, max_elapsed_s)
            now = datetime.now(UTC)
            self._operation_id = state.operation_id
            self._max_attempts = state.max_attempts
            self._started = time.monotonic() - max(0.0, (now - state.started_at).total_seconds())
            self._deadline = time.monotonic() + (state.deadline - now).total_seconds()
            self._prior_requests = state.request_count
            self._repair_used, self._context_used, self._empty_used = (
                state.repair_used,
                state.context_used,
                state.empty_used,
            )
            self._protocols = {(provider, model): protocol for provider, model, protocol in state.protocols}
            self._transport_retries = set(state.transport_retries)

    @property
    def request_count(self) -> int:
        """已保留的历史请求与本轮已结束请求数量，历史未确认请求同样占用额度。"""
        return self._prior_requests + len(self._attempts)

    @property
    def next_number(self) -> int:
        """当前发送和结束回执共同使用的连续请求编号。"""
        return self.request_count + 1

    @property
    def last_failure(self) -> FailureCode | None:
        """保留未发送也可能发生的明确失败分类。"""
        return self._last_failure

    def note_failure(self, error: FailureCode | None) -> None:
        """供正式调用边界传递失败，不构造请求或费用。"""
        self._last_failure = error

    def _save_allowances(self) -> None:
        if self._journal is not None:
            self._journal.save_allowances(
                repair=self._repair_used,
                context=self._context_used,
                empty=self._empty_used,
                protocols=self._protocols,
                transport_retries=self._transport_retries,
            )

    @property
    def operation_id(self) -> str:
        """标识同一共享预算，使跨对象成本记录可以去重。"""
        return self._operation_id

    @property
    def elapsed_s(self) -> float:
        """返回整次操作的真实累计时间。"""
        return max(0.0, time.monotonic() - self._started)

    @property
    def remaining_s(self) -> float:
        """返回所有后续步骤共用的剩余时间。"""
        return max(0.0, self._deadline - time.monotonic())

    @property
    def attempts(self) -> tuple[ModelAttempt, ...]:
        """返回不可增删的实际请求记录视图。"""
        return tuple(self._attempts)

    def interruption(self, *, before_request: bool = True, force_identity: bool = True) -> FailureCode | None:
        """发送前与等待期间分别检查取消、截止时间及实际请求数量。"""
        if cancellation_requested(force=force_identity) or (
            self._cancelled is not None and self._cancelled.poll(force=force_identity)
        ):
            return "cancelled"
        if self.remaining_s <= 0:
            return "deadline"
        if before_request and self.request_count >= self._max_attempts:
            return "attempt_budget"
        return None

    @contextmanager
    def operation(self) -> Iterator[None]:
        """拒绝并发或递归复用同一编辑预算，避免计数与记录竞争。"""
        if not self._operation.acquire(blocking=False):
            raise RuntimeError("同一编辑预算只允许串行调用")
        try:
            yield
        finally:
            self._operation.release()

    def record(self, attempt: ModelAttempt) -> None:
        """保存一次已结束的真实请求，保持连续序号和硬次数上限。"""
        if self.request_count >= self._max_attempts or attempt.number != self.next_number:
            raise ValueError("模型请求记录超出次数上限或序号不连续")
        self._attempts.append(ModelAttempt.model_validate(attempt))
        if self._journal is not None:
            self._journal.finish(self.operation_id, attempt)

    def start_request(
        self,
        *,
        provider_id: str,
        connection_id: str,
        model: str,
        purpose: ModelPurpose,
        protocol: ModelProtocol,
        output_tokens_limit: int | None,
        request: object,
    ) -> None:
        """实际请求边界登记身份；纯计算预算不访问数据库。"""
        if self._journal is not None:
            self._journal.start(
                operation_id=self.operation_id,
                number=self.next_number,
                provider_id=provider_id,
                connection_id=connection_id,
                model=model,
                purpose=purpose,
                protocol=protocol,
                output_tokens_limit=output_tokens_limit,
                request=request,
            )

    def request_receipt(self, usage: TokenUsage, request_id: str | None) -> None:
        """保留已经收到的实际用量；后续业务失败不会抹除该回执。"""
        if self._journal is not None:
            self._journal.receipt(self.operation_id, self.next_number, usage, request_id)

    def save_search(self, evidence: SearchEvidence) -> None:
        """搜索与后续生成共用请求账本；来源写入失败时不能继续发送。"""
        if self._journal is not None:
            self._journal.save_search(self.operation_id, self.next_number, evidence)
        self._search_results.append(evidence)

    @property
    def search_results(self) -> tuple[SearchEvidence, ...]:
        """返回当前执行取得的真实搜索来源，供业务结果保存引用。"""
        return tuple(self._search_results)

    def claim_repair(self) -> bool:
        """整个操作最多允许一次格式或引用修复，不因路由切换重新授予。"""
        if self._repair_used or self.interruption() is not None:
            return False
        self._repair_used = True
        self._save_allowances()
        return True

    def claim_context(self) -> bool:
        """整个操作最多允许一次实际上下文扩展。"""
        if self._context_used or self.interruption() is not None:
            return False
        self._context_used = True
        self._save_allowances()
        return True

    def claim_empty_retry(self) -> bool:
        """空正文重试同样共享一次性额度。"""
        if self._empty_used or self.interruption() is not None:
            return False
        self._empty_used = True
        self._save_allowances()
        return True

    def claim_transport_retry(self, connection_id: str, endpoint: str, model: str) -> bool:
        """同一连接、端点和模型在整个操作中最多补发一次暂态失败请求。"""
        identity = (connection_id, hashlib.sha256(endpoint.encode()).hexdigest(), model)
        if identity in self._transport_retries or self.interruption() is not None:
            return False
        self._transport_retries.add(identity)
        self._save_allowances()
        return True

    def protocol(self, provider_id: str, model: str, *, initial: ModelProtocol = "json_schema") -> ModelProtocol:
        """同次操作记住初始能力及已协商格式，避免后续步骤重复发送被拒绝的协议。"""
        identity = (provider_id, model)
        if identity not in self._protocols:
            self._protocols[identity] = initial
            self._save_allowances()
        return self._protocols[identity]

    def downgrade_protocol(self, provider_id: str, model: str) -> bool:
        """仅由明确不支持格式的调用分支使用，仍保留本地严格校验。"""
        current = self.protocol(provider_id, model)
        if current == "text":
            return False
        self._protocols[(provider_id, model)] = "json_object" if current == "json_schema" else "text"
        self._save_allowances()
        return True
