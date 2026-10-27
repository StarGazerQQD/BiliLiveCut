# ruff: noqa: D100
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypedDict, cast

from pydantic import JsonValue

JobStatus = Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]
ACTIVE = frozenset({"queued", "running", "cancelling"})
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
FORMAT_VERSION = 1
BUILTIN_PAYLOAD_FIELDS = {
    "editorial_reanalysis": {"session_id", "targets"},
    "local_import": {"import_id"},
    "candidate_render": {"candidate_id", "reviewed_by"},
    "review_rerender": {"candidate_id", "start_ts", "end_ts", "version"},
    "collection_render": {"topic_id", "event_ids", "chapter_titles", "include_chapter_cards"},
    "clip_upload": {"clip_id"},
    "upload_retry": {"upload_task_id"},
}


class JobData(TypedDict):
    """持久作业与 HTTP 查询共享的版本化记录。"""

    version: int
    id: str
    type: str
    label: str
    owner: str
    payload: dict[str, JsonValue]
    dedup_key: str | None
    cancellable_while_running: bool
    status: JobStatus
    progress: int
    message: str
    result: dict[str, JsonValue] | None
    error: str | None
    attempt: int
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    recovered: bool


@dataclass(frozen=True, slots=True)
class JobAttempt:
    """仅授权一个作业的当前执行代次。"""

    job_id: str
    number: int


class JobCancelled(RuntimeError):
    """作业收到取消请求，禁止继续发布结果。"""


class JobSuperseded(JobCancelled):
    """作业代次或状态已改变，拒绝迟到写入。"""


def now_iso() -> str:
    """返回持久记录使用的 UTC 时间。"""
    return datetime.now(UTC).isoformat()


def validate_payload(job_type: str, payload: dict[str, JsonValue]) -> None:
    """严格验证内置操作请求的字段集合和 JSON 可编码性。"""
    fields = BUILTIN_PAYLOAD_FIELDS.get(job_type)
    if fields is not None and set(payload) != fields:
        raise ValueError(f"Web 作业 {job_type} payload 字段不符合当前格式")
    json.dumps(payload, allow_nan=False)


def decode_job(raw: str) -> JobData:
    """解析并验证既有持久格式，不推测缺失字段。"""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Web 作业记录不是有效 JSON") from exc
    validate_job(value)
    return cast(JobData, value)


def validate_job(value: object) -> None:
    """验证公开字段、类型和时间，保留格式版本 1 的读取契约。"""
    if not isinstance(value, dict) or set(value) != set(JobData.__annotations__):
        raise ValueError("Web 作业记录字段不符合当前格式")
    if type(value["version"]) is not int or value["version"] != FORMAT_VERSION:
        raise ValueError(f"Web 作业记录版本必须为 {FORMAT_VERSION}")
    for name in ("id", "type", "label", "owner", "message"):
        if not isinstance(value[name], str) or not value[name]:
            raise ValueError(f"Web 作业字段 {name} 必须是非空字符串")
    if not isinstance(value["payload"], dict):
        raise ValueError("Web 作业 payload 必须是对象")
    validate_payload(value["type"], value["payload"])
    if value["dedup_key"] is not None and not isinstance(value["dedup_key"], str):
        raise ValueError("Web 作业 dedup_key 必须是字符串或 null")
    for name in ("cancellable_while_running", "recovered"):
        if not isinstance(value[name], bool):
            raise ValueError(f"Web 作业字段 {name} 必须是布尔值")
    if not isinstance(value["status"], str) or value["status"] not in ACTIVE | TERMINAL:
        raise ValueError("Web 作业 status 无效")
    if type(value["progress"]) is not int or not 0 <= value["progress"] <= 100:
        raise ValueError("Web 作业 progress 必须是 0..100 的整数")
    if type(value["attempt"]) is not int or value["attempt"] < 1:
        raise ValueError("Web 作业 attempt 必须是正整数")
    if value["result"] is not None and not isinstance(value["result"], dict):
        raise ValueError("Web 作业 result 必须是对象或 null")
    if value["error"] is not None and not isinstance(value["error"], str):
        raise ValueError("Web 作业 error 必须是字符串或 null")
    for name in ("created_at", "updated_at", "started_at", "finished_at"):
        if value[name] is None and name in {"started_at", "finished_at"}:
            continue
        if not isinstance(value[name], str) or not value[name]:
            raise ValueError(f"Web 作业字段 {name} 必须是 ISO 时间")
        try:
            datetime.fromisoformat(value[name])
        except ValueError as exc:
            raise ValueError(f"Web 作业字段 {name} 必须是 ISO 时间") from exc
    json.dumps(value, allow_nan=False)
