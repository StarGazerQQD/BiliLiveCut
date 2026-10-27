# ruff: noqa: D100
from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Generic, TypeVar
from uuid import uuid4

from app.analysis.editorial_contracts import FailureCode, ModelAttempt, ModelProtocol

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class StructuredCallResult(Generic[T]):
    """只携带通过业务验证的值，失败时保留分类而不构造业务结论。"""

    value: T | None = field(repr=False)
    error: FailureCode | None

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
    ) -> None:
        if type(max_attempts) is not int or not 1 <= max_attempts <= 32:
            raise ValueError("模型总请求次数必须在 1~32 之间")
        if isinstance(max_elapsed_s, bool) or not math.isfinite(max_elapsed_s) or max_elapsed_s <= 0:
            raise ValueError("模型总时间预算必须为有限正数")
        self._max_attempts = max_attempts
        self._operation_id = uuid4().hex
        self._started = time.monotonic()
        self._deadline = self._started + max_elapsed_s
        self._cancelled = cancelled
        self._attempts: list[ModelAttempt] = []
        self._repair_used = False
        self._context_used = False
        self._empty_used = False
        self._operation = Lock()
        self._protocols: dict[tuple[str, str], ModelProtocol] = {}

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

    def interruption(self, *, before_request: bool = True) -> FailureCode | None:
        """发送前与等待期间分别检查取消、截止时间及实际请求数量。"""
        if self._cancelled is not None and self._cancelled():
            return "cancelled"
        if self.remaining_s <= 0:
            return "deadline"
        if before_request and len(self._attempts) >= self._max_attempts:
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
        if len(self._attempts) >= self._max_attempts or attempt.number != len(self._attempts) + 1:
            raise ValueError("模型请求记录超出次数上限或序号不连续")
        self._attempts.append(ModelAttempt.model_validate(attempt))

    def claim_repair(self) -> bool:
        """整个操作最多允许一次格式或引用修复，不因路由切换重新授予。"""
        if self._repair_used or self.interruption() is not None:
            return False
        self._repair_used = True
        return True

    def claim_context(self) -> bool:
        """整个操作最多允许一次实际上下文扩展。"""
        if self._context_used or self.interruption() is not None:
            return False
        self._context_used = True
        return True

    def claim_empty_retry(self) -> bool:
        """空正文重试同样共享一次性额度。"""
        if self._empty_used or self.interruption() is not None:
            return False
        self._empty_used = True
        return True

    def protocol(self, provider_id: str, model: str) -> ModelProtocol:
        """同次操作记住服务明确拒绝的格式，默认先请求严格模式。"""
        return self._protocols.get((provider_id, model), "json_schema")

    def downgrade_protocol(self, provider_id: str, model: str) -> bool:
        """仅由明确不支持格式的调用分支使用，仍保留本地严格校验。"""
        current = self.protocol(provider_id, model)
        if current == "text":
            return False
        self._protocols[(provider_id, model)] = "json_object" if current == "json_schema" else "text"
        return True
