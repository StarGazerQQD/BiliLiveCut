# ruff: noqa: D100
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlmodel import Session, select

from app.db import job_store
from app.db.entities import AppSetting, UploadAttempt, UploadStatus, UploadTask
from app.jobs.contracts import JobAttempt, JobData


class UploadJobBinding(BaseModel):
    """页面执行代次对应的上传事实，不通过 clip ID 推断其他执行的成功。"""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, le=1)
    upload_task_id: int
    clip_id: int
    generation: int
    attempt_token: str | None
    uploader: str


def bind_upload_job(db: Session, job_attempt: JobAttempt, upload: UploadTask, attempt: UploadAttempt | None) -> None:
    """在领取上传的同一写事务绑定当前页面作业，先于外部副作用。"""
    job = job_store.require_running(db, job_attempt)
    if (
        job["type"] == "clip_upload"
        and job["payload"]["clip_id"] == upload.clip_id
        or job["type"] == "upload_retry"
        and job["payload"]["upload_task_id"] == upload.id
    ):
        if upload.id is None or attempt is None and upload.uploader != "manual":
            raise ValueError("上传作业缺少可核对的执行记录")
        binding = UploadJobBinding(
            upload_task_id=upload.id,
            clip_id=upload.clip_id,
            generation=attempt.publish_generation if attempt else upload.publish_generation,
            attempt_token=attempt.attempt_token if attempt else None,
            uploader=upload.uploader,
        )
        key = f"upload_job:v1:{job_attempt.job_id}:{job_attempt.number}"
        row = db.get(AppSetting, key) or AppSetting(key=key)
        row.value = binding.model_dump_json()
        db.add(row)
        return
    raise ValueError("页面作业与上传来源不一致")


def _confirmed_result(db: Session, job: JobData) -> dict[str, JsonValue] | None:
    row = db.get(AppSetting, f"upload_job:v1:{job['id']}:{job['attempt']}")
    if row is None:
        return None
    binding = UploadJobBinding.model_validate_json(row.value)
    upload = db.get(UploadTask, binding.upload_task_id)
    if (
        upload is None
        or upload.clip_id != binding.clip_id
        or upload.uploader != binding.uploader
        or upload.publish_generation != binding.generation
        or upload.status != UploadStatus.SUCCESS
    ):
        return None
    if (
        job["type"] == "clip_upload"
        and job["payload"]["clip_id"] != binding.clip_id
        or job["type"] == "upload_retry"
        and job["payload"]["upload_task_id"] != binding.upload_task_id
    ):
        return None
    if binding.attempt_token is not None:
        attempt = db.exec(
            select(UploadAttempt).where(
                UploadAttempt.attempt_token == binding.attempt_token,
                UploadAttempt.upload_task_id == binding.upload_task_id,
                UploadAttempt.publish_generation == binding.generation,
                UploadAttempt.clip_id == binding.clip_id,
            )
        ).first()
        if (
            attempt is None
            or attempt.status != UploadStatus.SUCCESS
            or not attempt.remote_id
            or attempt.remote_id != upload.remote_id
        ):
            return None
    elif upload.uploader != "manual" or upload.remote_id != "manual":
        return None
    return {"task_id": binding.upload_task_id, "status": "success", "error": None}


def recover_upload_job(attempt: JobAttempt) -> JobData:
    """仅回放已提交的上传结果；未知、失效或取消执行从不重发请求。"""
    return job_store.recover(attempt, resolve=_confirmed_result)
