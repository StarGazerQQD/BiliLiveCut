"""持久化日志 — 远程成功结果在 DB 不可用时的兜底。

当远程上传已返回 SUCCESS 但本地 DB 无法提交时 (如崩溃、连接断开),
将结果写入此 Journal。重启后由 publish_recovery 回填 DB。

Journal 不包含: Cookie, Authorization, API Key, 完整请求头, 敏感账号凭据。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

_JOURNAL_DIR = Path(os.environ.get("BLC_JOURNAL_DIR", "storage/journal"))


def _journal_path() -> Path:
    """确保 Journal 目录存在并返回当日日志文件路径。"""
    _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).strftime("%Y%m%d")
    return _JOURNAL_DIR / f"publish_journal_{today}.jsonl"


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
    """持久化远程成功结果 (行式 JSON, 可追加)。

    调用时机: 远程返回 SUCCESS 但 DB submit 失败时。

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
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("journal_write: attempt=%s remote=%s → %s", attempt_token, remote_id, path)
        return True
    except OSError as exc:
        logger.error("journal_write_failed: attempt=%s error=%s", attempt_token, exc)
        return False


def read_pending_entries() -> list[dict]:
    """读取所有尚未回填的 Journal 条目。

    :returns: 条目列表 (按时间顺序)。
    """
    entries: list[dict] = []
    if not _JOURNAL_DIR.exists():
        return entries

    for path in sorted(_JOURNAL_DIR.glob("publish_journal_*.jsonl")):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("journal_read_error: %s error=%s", path, exc)

    return entries


def mark_replayed(attempt_token: str, publish_generation: int) -> bool:
    """标记某个 Journal 条目已被回填 (删除对应行)。

    当前实现: 重写整个文件去除对应行。

    :param attempt_token: 已回填的 attempt 令牌。
    :param publish_generation: 对应代数。
    :returns: True 表示操作成功。
    """
    for path in sorted(_JOURNAL_DIR.glob("publish_journal_*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines: list[str] = []
            for line in lines:
                line_s = line.strip()
                if not line_s:
                    continue
                try:
                    entry = json.loads(line_s)
                except json.JSONDecodeError:
                    new_lines.append(line)
                    continue
                if (
                    entry.get("attempt_token") == attempt_token
                    and entry.get("publish_generation") == publish_generation
                ):
                    continue
                new_lines.append(line)
            if len(new_lines) < len(lines):
                path.write_text("".join(new_lines), encoding="utf-8")
                return True
        except OSError as exc:
            logger.warning("journal_mark_replayed_failed: %s error=%s", path, exc)

    return False
