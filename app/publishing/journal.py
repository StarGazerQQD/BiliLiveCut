"""持久化日志 — 远程成功结果在业务提交前的持久回执。

远程上传返回 SUCCESS 后立即写入此 Journal，随后提交业务数据库。
重启后由 publish_recovery 回填尚未完成提交的成功结果。

Journal 不包含: Cookie, Authorization, API Key, 完整请求头, 敏感账号凭据。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from loguru import logger

_JOURNAL_DIR = Path(os.environ.get("BLC_JOURNAL_DIR", "storage/journal"))


def _journal_path() -> Path:
    """每次写入独立文件，避免回放重写覆盖同时追加的成功结果。"""
    _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).strftime("%Y%m%d")
    return _JOURNAL_DIR / f"publish_journal_{today}_{uuid4().hex}.json"


def _atomic_write(path: Path, content: str) -> None:
    """先落盘再原子替换；读者只会看到完整的日志。"""
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_remote_success(
    attempt_token: str,
    publish_generation: int,
    upload_task_id: int,
    clip_id: int,
    remote_id: str,
    remote_url: str | None = None,
    platform: str = "bilibili",
    finished_at: str | None = None,
) -> bool:
    """持久化远程成功结果（独立 JSON 文件，原子写入）。

    调用时机: 远程返回 SUCCESS 后、业务提交前；也保留 DB submit 失败后的补写入口。

    不含敏感凭据 (Cookie/Token/API Key)。

    :param attempt_token: UploadAttempt 追踪令牌。
    :param publish_generation: 发布代数。
    :param upload_task_id: UploadTask ID。
    :param clip_id: FinalClip ID。
    :param remote_id: 平台稿件 ID。
    :param remote_url: 平台稿件链接 (可选)。
    :param platform: 投稿平台 (默认 bilibili)。
    :param finished_at: 完成时间 ISO 字符串 (可选, 默认当前时间)。
    :returns: True 表示写入成功。
    """
    entry = {
        "attempt_token": attempt_token,
        "publish_generation": publish_generation,
        "upload_task_id": upload_task_id,
        "clip_id": clip_id,
        "outcome": "success",
        "remote_id": remote_id,
        "remote_url": remote_url or "",
        "platform": platform,
        "journaled_at": finished_at or datetime.now(UTC).isoformat(),
    }

    try:
        path = _journal_path()
        _atomic_write(path, json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("journal_write: attempt={} remote={} → {}", attempt_token, remote_id, path)
        return True
    except OSError as exc:
        logger.error("journal_write_failed: attempt={} error={}", attempt_token, exc)
        return False


def _read_entry(path: Path) -> dict[str, str | int | None] | None:
    """只读取当前单条 JSON 文件，损坏文件留在原处供排查。"""
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        logger.warning("journal_read_error: {} error={}", path, exc)
        return None
    if isinstance(entry, dict) and all(value is None or isinstance(value, (str, int)) for value in entry.values()):
        return entry
    return None


def read_pending_entries() -> list[dict[str, str | int | None]]:
    """按文件时间顺序读取当前独立日志，不读取或迁移旧多行日志。"""
    return [
        entry
        for path in sorted(_JOURNAL_DIR.glob("publish_journal_*.json"))
        if (entry := _read_entry(path)) is not None
    ]


def mark_replayed(
    attempt_token: str, publish_generation: int, *, remote_id: str | None = None, remote_url: str | None = None
) -> bool:
    """回填成功后仅删除对应独立文件，不重写其他成功结果。"""
    for path in sorted(_JOURNAL_DIR.glob("publish_journal_*.json")):
        entry = _read_entry(path)
        if (
            entry is not None
            and entry.get("attempt_token") == attempt_token
            and entry.get("publish_generation") == publish_generation
            and (
                remote_id is None
                or (entry.get("remote_id") == remote_id and (entry.get("remote_url") or "") == (remote_url or ""))
            )
        ):
            try:
                path.unlink(missing_ok=True)
                return True
            except OSError as exc:
                logger.warning("journal_mark_replayed_failed: {} error={}", path, exc)
    return False
