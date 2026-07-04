"""上传队列与可插拔上传器。

合规设计(重要):

* **默认 manual**:不调用任何平台接口,只产出成品 + 待上传清单,**零封号风险**;
* **biliup 为可选、默认关闭**:由用户在 Web 后台显式开启(``settings_store``),
  且需自行配置上传命令(``BILIUP_UPLOAD_CMD``)与凭据,**风险自负**;
* 本模块不实现任何绕过平台安全策略、验证码或风控的逻辑。

上传前置校验:文件完整性、标题/简介合规、内容查重、投稿频率限制;
执行:失败重试、状态机、日志,全部记录在 ``upload_tasks``。
"""

from __future__ import annotations

import shlex
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from loguru import logger
from sqlmodel import select

from app.core import settings_store
from app.core.config import settings
from app.db.models import (
    FinalClip,
    UploadStatus,
    UploadTask,
    utcnow,
)
from app.db.session import get_session


@dataclass(slots=True)
class PrecheckResult:
    """上传前置校验结果。

    :param ok: 是否通过全部校验。
    :param reasons: 未通过的原因列表。
    """

    ok: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class UploadResult:
    """一次上传执行的结果。

    :param success: 是否成功。
    :param remote_id: 平台返回的稿件号(若有)。
    :param message: 说明信息。
    """

    success: bool
    remote_id: str | None = None
    message: str = ""


# --------------------------------------------------------------------------- #
# 前置校验
# --------------------------------------------------------------------------- #
def precheck_clip(clip_id: int) -> PrecheckResult:
    """对成品切片做上传前置校验。

    校验项:文件存在且非空、标题/简介存在且不超长、内容未重复上传、未超投稿频率。

    :param clip_id: ``final_clips`` 主键。
    :returns: :class:`PrecheckResult`。
    """
    reasons: list[str] = []
    with get_session() as db:
        clip = db.get(FinalClip, clip_id)
        if clip is None:
            return PrecheckResult(ok=False, reasons=["切片不存在"])
        file_path = clip.file_path
        title = clip.title or ""
        desc = clip.description or ""
        content_hash = clip.content_hash

    # 1) 文件完整性
    p = Path(file_path)
    if not p.exists() or p.stat().st_size == 0:
        reasons.append("成品文件缺失或为空")

    # 2) 标题/简介合规
    if not title.strip():
        reasons.append("标题为空")
    elif len(title) > settings.title_max_len:
        reasons.append(f"标题超长(>{settings.title_max_len})")
    if not desc.strip():
        reasons.append("简介为空")
    elif len(desc) > settings.desc_max_len:
        reasons.append(f"简介超长(>{settings.desc_max_len})")

    # 3) 内容查重:相同内容指纹且已有成功上传
    if content_hash and _hash_already_uploaded(content_hash, exclude_clip_id=clip_id):
        reasons.append("相同内容已成功上传(查重命中)")

    # 4) 投稿频率限制
    if _recent_success_count(hours=1) >= settings.upload_max_per_hour:
        reasons.append(f"超过投稿频率上限(每小时 {settings.upload_max_per_hour})")

    return PrecheckResult(ok=not reasons, reasons=reasons)


def _hash_already_uploaded(content_hash: str, exclude_clip_id: int) -> bool:
    """判断某内容指纹是否已有成功上传记录。

    :param content_hash: 内容指纹。
    :param exclude_clip_id: 排除的切片 id(自身)。
    :returns: 已上传返回 ``True``。
    """
    with get_session() as db:
        clip_ids = [
            c.id
            for c in db.exec(
                select(FinalClip).where(FinalClip.content_hash == content_hash)
            ).all()
            if c.id != exclude_clip_id
        ]
        if not clip_ids:
            return False
        tasks = db.exec(
            select(UploadTask).where(
                UploadTask.clip_id.in_(clip_ids),  # type: ignore[attr-defined]
                UploadTask.status == UploadStatus.SUCCESS,
            )
        ).all()
    return len(tasks) > 0


def _recent_success_count(hours: int) -> int:
    """统计最近一段时间内成功上传的任务数(用于频控)。

    :param hours: 时间窗口(小时)。
    :returns: 成功任务数。
    """
    since = utcnow() - timedelta(hours=hours)
    with get_session() as db:
        rows = db.exec(
            select(UploadTask).where(
                UploadTask.status == UploadStatus.SUCCESS,
                UploadTask.updated_at >= since,  # type: ignore[arg-type]
            )
        ).all()
    return len(rows)


# --------------------------------------------------------------------------- #
# 上传器
# --------------------------------------------------------------------------- #
class Uploader(ABC):
    """上传器抽象接口。任何实现都接收一个 clip 字典并返回 :class:`UploadResult`。"""

    name: str = "base"

    @abstractmethod
    def upload(self, clip: dict) -> UploadResult:
        """执行上传。

        :param clip: 含 ``file_path`` / ``title`` / ``description`` 等键的字典。
        :returns: :class:`UploadResult`。
        """
        raise NotImplementedError


class ManualUploader(Uploader):
    """手动上传器(默认,零风险)。

    不调用任何平台接口,只确保成品与元数据已就绪(导出待上传清单),
    交由用户在 B 站官方"创作中心/必剪"手动投稿。
    """

    name = "manual"

    def upload(self, clip: dict) -> UploadResult:
        """导出清单并标记为就绪。

        :param clip: 切片字典。
        :returns: :class:`UploadResult`(始终成功,remote_id 标记为 manual)。
        """
        from app.publishing.copywriter import export_manifest

        export_manifest(clip["id"])
        msg = "manual 模式:已导出待上传清单,请在 B 站官方渠道手动投稿(未调用任何平台接口)。"
        logger.info("[manual] clip={} {}", clip["id"], msg)
        return UploadResult(success=True, remote_id="manual", message=msg)


class BiliupUploader(Uploader):
    """biliup 上传器(社区方案,默认关闭,风险自负)。

    仅当用户在 Web 后台开启开关、并配置了 ``BILIUP_UPLOAD_CMD`` 时才会真正执行;
    否则以清晰提示安全失败。本类不内置任何凭据处理或风控绕过逻辑。
    """

    name = "biliup"

    def upload(self, clip: dict) -> UploadResult:
        """通过用户配置的命令模板执行 biliup 上传。

        :param clip: 切片字典。
        :returns: :class:`UploadResult`。
        """
        logger.warning(
            "[biliup] 合规提示:biliup 使用你自己的登录态走网页投稿端点,"
            "可能违反平台条款并触发风控/封号,风险自负。clip={}",
            clip["id"],
        )
        template = settings.biliup_upload_cmd.strip()
        if not template:
            return UploadResult(
                success=False,
                message="未配置 BILIUP_UPLOAD_CMD,biliup 上传未执行(合规风险自负)。",
            )

        # V0.1.8.2:使用 shlex.quote 包裹参数,防止命令注入。
        import shlex as _shlex
        cmd_str = template.format(
            file=_shlex.quote(str(clip["file_path"])),
            title=_sanitize_biliup(clip.get("title") or ""),
            desc=_sanitize_biliup(clip.get("description") or ""),
        )
        try:
            args = shlex.split(cmd_str, posix=False)
            proc = subprocess.run(args, capture_output=True, timeout=1800)
        except Exception as exc:  # noqa: BLE001 — 外部命令任何异常都转为失败结果
            return UploadResult(success=False, message=f"biliup 命令执行异常: {exc}")

        out = (proc.stdout or b"").decode("utf-8", errors="ignore")
        err = (proc.stderr or b"").decode("utf-8", errors="ignore")
        if proc.returncode != 0:
            return UploadResult(
                success=False,
                message=f"biliup 失败(code={proc.returncode}): {err[:300]}",
            )
        # 尽力从输出解析稿件号(BV 号)。
        remote_id = _parse_bv(out) or _parse_bv(err)
        return UploadResult(success=True, remote_id=remote_id, message="biliup 上传完成。")


def _sanitize_biliup(value: str) -> str:
    """对 biliup 命令行参数做安全清洗:仅保留安全字符。"""
    import re as _re
    return _re.sub(r"[^\w\u4e00-\u9fff\u3000-\u303f\uff00-\uffef .,!?()（）《》\[\]【】\-+#&;:/@]", "", value)


def _parse_bv(text: str) -> str | None:
    """从文本中尽力提取 BV 号。

    :param text: 命令输出文本。
    :returns: BV 号或 ``None``。
    """
    import re

    m = re.search(r"BV[0-9A-Za-z]{8,}", text)
    return m.group(0) if m else None


def get_uploader() -> Uploader:
    """按运行时开关选择上传器。

    biliup 开关开启时返回 :class:`BiliupUploader`,否则返回 :class:`ManualUploader`。

    :returns: 上传器实例。
    """
    if settings_store.biliup_enabled():
        return BiliupUploader()
    return ManualUploader()


# --------------------------------------------------------------------------- #
# 队列
# --------------------------------------------------------------------------- #
def enqueue_upload(clip_id: int) -> UploadTask:
    """对一个成品切片做前置校验并创建上传任务。

    校验未通过时仍创建任务但状态为 ``skipped`` 并记录原因,便于在后台查看。

    :param clip_id: ``final_clips`` 主键。
    :returns: 创建的 :class:`UploadTask`。
    """
    import json

    pre = precheck_clip(clip_id)
    uploader_name = "biliup" if settings_store.biliup_enabled() else "manual"
    status = UploadStatus.QUEUED if pre.ok else UploadStatus.SKIPPED
    task = UploadTask(
        clip_id=clip_id,
        uploader=uploader_name,
        status=status,
        precheck_json=json.dumps({"ok": pre.ok, "reasons": pre.reasons}, ensure_ascii=False),
        last_error=None if pre.ok else ";".join(pre.reasons),
    )
    with get_session() as db:
        db.add(task)
        db.flush()
        db.refresh(task)
        tid = task.id
    if pre.ok:
        logger.info("上传任务入队 task={} clip={} uploader={}", tid, clip_id, uploader_name)
    else:
        logger.warning("上传预检未通过 clip={} 原因={}", clip_id, pre.reasons)
    return task


def process_upload_task(task_id: int) -> UploadTask:
    """执行一个上传任务(带重试)。

    :param task_id: ``upload_tasks`` 主键。
    :returns: 更新后的 :class:`UploadTask`。
    :raises ValueError: 任务不存在时。
    """
    with get_session() as db:
        task = db.get(UploadTask, task_id)
        if task is None:
            raise ValueError(f"上传任务不存在: id={task_id}")
        if task.status == UploadStatus.SKIPPED:
            return task
        clip = db.get(FinalClip, task.clip_id)
        clip_dict = (
            {
                "id": clip.id,
                "file_path": clip.file_path,
                "title": clip.title,
                "description": clip.description,
            }
            if clip
            else None
        )

    if clip_dict is None:
        return _finish_task(task_id, UploadStatus.FAILED, error="切片不存在")

    uploader = get_uploader()
    last_error = ""
    for attempt in range(1, settings.upload_max_retries + 2):
        _set_task_running(task_id, attempt)
        try:
            result = uploader.upload(clip_dict)
        except Exception as exc:  # noqa: BLE001
            result = UploadResult(success=False, message=str(exc))
        if result.success:
            return _finish_task(
                task_id, UploadStatus.SUCCESS, remote_id=result.remote_id, error=None
            )
        last_error = result.message
        logger.warning("上传失败 task={} 第{}次: {}", task_id, attempt, last_error)

    return _finish_task(task_id, UploadStatus.FAILED, error=last_error)


def _set_task_running(task_id: int, attempt: int) -> None:
    """把任务标记为上传中并记录尝试次数。"""
    with get_session() as db:
        task = db.get(UploadTask, task_id)
        if task is not None:
            task.status = UploadStatus.UPLOADING
            task.attempts = attempt
            task.updated_at = utcnow()
            db.add(task)


def _finish_task(
    task_id: int,
    status: str,
    remote_id: str | None = None,
    error: str | None = None,
) -> UploadTask:
    """落地任务最终状态。

    :param task_id: 任务 id。
    :param status: 最终状态。
    :param remote_id: 稿件号。
    :param error: 错误信息。
    :returns: 更新后的任务。
    """
    from app.db.models import ClipStatus

    with get_session() as db:
        task = db.get(UploadTask, task_id)
        if task is None:
            raise ValueError(f"上传任务不存在: id={task_id}")
        task.status = status
        task.remote_id = remote_id
        task.last_error = error
        task.updated_at = utcnow()
        db.add(task)
        # 成功则把对应成品标记为已发布。
        if status == UploadStatus.SUCCESS:
            clip = db.get(FinalClip, task.clip_id)
            if clip is not None:
                clip.status = ClipStatus.PUBLISHED
                db.add(clip)
        db.refresh(task)
        return task


def enqueue_and_upload(clip_id: int) -> UploadTask:
    """入队并立即执行上传(便于自动链路与 CLI 复用)。

    :param clip_id: 成品切片 id。
    :returns: 上传任务。
    """
    task = enqueue_upload(clip_id)
    if task.status == UploadStatus.QUEUED and task.id is not None:
        return process_upload_task(task.id)
    return task
