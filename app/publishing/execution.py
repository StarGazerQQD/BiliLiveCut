# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from app.db import job_store
from app.db.entities import AppSetting, FinalClip, UploadAttempt, UploadTask
from app.jobs.contracts import JobAttempt, JobSuperseded
from app.pipeline.lease import TaskLease


def _stamp(path: str) -> tuple[int, int, int, int, int]:
    stat = Path(path).stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@dataclass(frozen=True)
class UploadFile:
    """写事务外读取完整内容，写事务内只复验同一文件对象。"""

    path: str
    sha256: str
    stamp: tuple[int, int, int, int, int]

    @classmethod
    def inspect(cls, path: str) -> UploadFile:
        """捕获实际发送文件的内容身份，不依赖旧成片摘要或仅文件大小。"""
        stamp = _stamp(path)
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        result = cls(path, digest.hexdigest(), stamp)
        result.require_unchanged(path)
        return result

    def require_unchanged(self, path: str) -> None:
        """拒绝预检后改动的上传文件。"""
        if path != self.path or _stamp(path) != self.stamp:
            raise ValueError("上传文件在准备后发生改变")

    def fingerprint(self, clip: FinalClip) -> str:
        """绑定发送的真实文件与文案，所有字段使用无歧义编码。"""
        self.require_unchanged(clip.file_path)
        values = {
            "version": 1,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.stamp[2],
            "clip_id": clip.id,
            "candidate_id": clip.candidate_id,
            "content_hash": clip.content_hash,
            "title": clip.title,
            "description": clip.description,
            "tags": clip.tags_json,
        }
        return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class UploadExecution(BaseModel):
    """准备事务授予的唯一执行来源，缺少调用方凭据不能降级为直接调用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1, le=1)
    token: str
    generation: int
    upload_task_id: int
    clip_id: int
    worker_id: str
    uploader: str
    task_lease: TaskLease | None = None
    job_attempt: JobAttempt | None = None

    @property
    def key(self) -> str:
        """返回同尝试和代次的来源绑定键。"""
        return f"upload_execution:v1:{self.token}:{self.generation}"

    def matches(self, attempt: UploadAttempt, upload: UploadTask) -> bool:
        """心跳和发送共用已捕获的发布占用身份。"""
        return (
            attempt.attempt_token == self.token
            and attempt.publish_generation == self.generation
            and upload.publish_generation == self.generation
            and attempt.upload_task_id == upload.id == self.upload_task_id
            and attempt.clip_id == upload.clip_id == self.clip_id
            and attempt.created_by_worker == upload.claimed_by == self.worker_id
            and upload.uploader == self.uploader
            and attempt.lease_token == (self.task_lease.lease_token if self.task_lease else None)
        )

    def source_error(
        self,
        db: Session,
        *,
        task_lease: TaskLease | None,
        job_attempt: JobAttempt | None,
    ) -> str | None:
        """发送之前复验原来源的当前权限、审核与执行代次。"""
        if self.task_lease != task_lease or self.job_attempt != job_attempt:
            return "上传执行来源凭据与准备记录不一致"
        if task_lease is not None:
            from app.pipeline.workers.publish import _publish_source_error

            return _publish_source_error(db, task_lease, self.clip_id)
        if job_attempt is not None:
            try:
                job = job_store.require_running(db, job_attempt)
            except JobSuperseded:
                return "页面上传作业已结束或执行代次已改变"
            if not (
                job["type"] == "clip_upload"
                and job["payload"]["clip_id"] == self.clip_id
                or job["type"] == "upload_retry"
                and job["payload"]["upload_task_id"] == self.upload_task_id
            ):
                return "页面上传作业的输入来源已改变"
        return None


def bind_execution(
    db: Session,
    attempt: UploadAttempt,
    uploader: str,
    *,
    task_lease: TaskLease | None,
    job_attempt: JobAttempt | None,
) -> UploadExecution:
    """与 PREPARED 尝试同事务保存来源，不能同时授予两种执行身份。"""
    if task_lease is not None and job_attempt is not None:
        raise ValueError("上传执行不能同时绑定流水线和页面作业")
    binding = UploadExecution(
        token=attempt.attempt_token,
        generation=attempt.publish_generation,
        upload_task_id=attempt.upload_task_id,
        clip_id=attempt.clip_id,
        worker_id=attempt.created_by_worker,
        uploader=uploader,
        task_lease=task_lease,
        job_attempt=job_attempt,
    )
    db.add(AppSetting(key=binding.key, value=binding.model_dump_json()))
    return binding


def read_execution(db: Session, attempt: UploadAttempt) -> UploadExecution:
    """缺少绑定的旧准备记录不能自动推断为可发送。"""
    row = db.get(AppSetting, f"upload_execution:v1:{attempt.attempt_token}:{attempt.publish_generation}")
    if row is None:
        raise ValueError("上传尝试缺少已确认的执行来源")
    return UploadExecution.model_validate_json(row.value)
