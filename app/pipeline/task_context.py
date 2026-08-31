"""SegmentTask ``context_json`` 的 Event-first 持久化辅助函数。"""

from __future__ import annotations

import json
from collections.abc import Mapping

EVENT_FIRST_CONTEXT_KEY = "event_first"


def load_task_context(raw: str | None) -> dict[str, object]:
    """解析任务上下文；损坏或非对象 JSON 安全降级为空对象。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def event_first_context(raw: str | None) -> dict[str, object]:
    """读取 Event-first 子上下文，不暴露可变的原始对象。"""
    value = load_task_context(raw).get(EVENT_FIRST_CONTEXT_KEY)
    return dict(value) if isinstance(value, Mapping) else {}


def update_event_first_context(raw: str | None, **updates: object) -> str:
    """合并 Event-first 字段，同时保留 reanalysis 等其他任务上下文。"""
    payload = load_task_context(raw)
    current = payload.get(EVENT_FIRST_CONTEXT_KEY)
    event_first = dict(current) if isinstance(current, Mapping) else {}
    event_first.update(updates)
    payload[EVENT_FIRST_CONTEXT_KEY] = event_first
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
