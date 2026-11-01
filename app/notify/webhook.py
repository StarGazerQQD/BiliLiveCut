"""多通道通知/Webhook 模块P2)。

支持:
- 钉钉机器人 Webhook(含加签)
- 企业微信机器人 Webhook
- SMTP 邮件
- 飞书/Lark 应用机器人

典型调用点:
- 切片完成(clipping/core.py produce_clip 末尾)
- 磁盘告警(monitor 阈值触发)
- 任务永久失败(task worker)
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from app.core.config import settings
from app.core.sanitize import safe_exception_summary

if TYPE_CHECKING:
    from app.notify.delivery import NotificationRule

# 允许的 webhook 域名白名单。
_ALLOWED_WEBHOOK_DOMAINS = {"oapi.dingtalk.com", "qyapi.weixin.qq.com"}


def _validate_webhook(url: str, name: str = "") -> bool:
    """仅接受对应平台的 HTTPS 地址、标准端口和无 userinfo 的 URL。"""
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in _ALLOWED_WEBHOOK_DOMAINS
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    expected = {"钉钉": "oapi.dingtalk.com", "企微": "qyapi.weixin.qq.com"}.get(name)
    if not valid or (expected is not None and parsed.hostname != expected):
        logger.warning("{}webhook 地址无效，请使用平台 HTTPS 地址且不要包含用户凭据", name)
        return False
    return True


# --------------------------------------------------------------------------- #
# 钉钉机器人
# --------------------------------------------------------------------------- #


def _dingtalk_sign(secret: str) -> tuple[str, str]:
    """生成钉钉加签参数。

    :param secret: 钉钉机器人密钥。
    :returns: ``(timestamp, sign)``。
    """
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    sign = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    import base64

    sign_encoded = urllib.parse.quote_plus(base64.b64encode(sign).decode("utf-8"))
    return ts, sign_encoded


def send_dingtalk(title: str, text: str) -> bool:
    """发送钉钉机器人消息。

    :param title: 消息标题。
    :param text: Markdown 正文。
    :returns: 是否成功。
    """
    if not settings.dingtalk_webhook:
        return False
    webhook = settings.dingtalk_webhook
    if not _validate_webhook(webhook, "钉钉"):
        return False
    if settings.dingtalk_secret:
        ts, sign = _dingtalk_sign(settings.dingtalk_secret)
        parsed = urllib.parse.urlparse(webhook)
        params = urllib.parse.parse_qs(parsed.query)
        params.update({"timestamp": [ts], "sign": [sign]})
        webhook = parsed._replace(query=urllib.parse.urlencode(params, doseq=True)).geturl()

    import httpx

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title[:64],
            "text": f"## {title}\n\n{text}\n\n> BiliLiveCut {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC",
        },
    }
    try:
        resp = httpx.post(webhook, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if not isinstance(result, dict):
            raise ValueError(f"HTTP {resp.status_code}: webhook 返回非对象 JSON")
        if result.get("errcode") == 0:
            logger.info("钉钉通知已发送: {}", title)
            return True
        logger.warning(
            "钉钉通知失败 host=oapi.dingtalk.com HTTP={} errcode={}", resp.status_code, result.get("errcode")
        )
        return False
    except Exception as exc:
        logger.error(
            "钉钉通知异常 host=oapi.dingtalk.com HTTP={}: {}",
            resp.status_code if "resp" in locals() else None,
            safe_exception_summary(exc),
        )
        return False


# --------------------------------------------------------------------------- #
# 企业微信机器人
# --------------------------------------------------------------------------- #


def send_wecom(title: str, text: str) -> bool:
    """发送企业微信机器人消息。

    :param title: 消息标题。
    :param text: Markdown 正文。
    :returns: 是否成功。
    """
    if not settings.wecom_webhook:
        return False

    import httpx

    if not _validate_webhook(settings.wecom_webhook, "企微"):
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": f"## {title}\n{text}\n\n> BiliLiveCut {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC",
        },
    }
    try:
        resp = httpx.post(settings.wecom_webhook, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if not isinstance(result, dict):
            raise ValueError(f"HTTP {resp.status_code}: webhook 返回非对象 JSON")
        if result.get("errcode") == 0:
            logger.info("企业微信通知已发送: {}", title)
            return True
        logger.warning(
            "企业微信通知失败 host=qyapi.weixin.qq.com HTTP={} errcode={}", resp.status_code, result.get("errcode")
        )
        return False
    except Exception as exc:
        logger.error(
            "企业微信通知异常 host=qyapi.weixin.qq.com HTTP={}: {}",
            resp.status_code if "resp" in locals() else None,
            safe_exception_summary(exc),
        )
        return False


# --------------------------------------------------------------------------- #
# 邮件通知
# --------------------------------------------------------------------------- #


def send_email(subject: str, body: str) -> bool:
    """将邮件通知加入后台队列，发送前强制检查收件验证。

    :param subject: 邮件主题。
    :param body: HTML 正文。
    :returns: 是否接受投递，实际发信状态由邮件服务记录。
    """
    from app.notify.delivery import publish_email

    try:
        return publish_email(subject, body, html=True)
    except Exception:  # noqa: BLE001 — 通知故障不影响主任务，避免原始 SMTP 响应泄露凭据
        logger.warning("邮件提醒未发送，请检查邮箱验证、邮件配置及本地存储")
        return False


# --------------------------------------------------------------------------- #
# 统一通知入口
# --------------------------------------------------------------------------- #


def notify(title: str, body: str, *, rule: NotificationRule | None = None) -> None:
    """通过所有已配置通道发送通知。

    :param title: 通知标题。
    :param body: 通知正文(纯文本/Markdown)。
    """
    from app.notify.delivery import publish_notification

    publish_notification(title, body, rule=rule)


def notify_clip_complete(candidate_id: int, clip_path: str, duration_s: float) -> None:
    """切片完成通知。

    :param candidate_id: 候选 ID。
    :param clip_path: 成品文件路径。
    :param duration_s: 时长(秒)。
    """
    notify(
        f"切片完成 #cand{candidate_id}",
        f"**候选 #{candidate_id}** 已生成成品切片。\n- 时长: {duration_s:.0f}s\n- 文件: `{Path(clip_path).name}`",
        rule="notify_on_clip",
    )


def notify_disk_alert(free_gb: float, threshold_gb: int, raw_gb: float, clips_gb: float) -> None:
    """磁盘空间不足告警。

    :param free_gb: 剩余空间(GB)。
    :param threshold_gb: 告警阈值(GB)。
    :param raw_gb: 原始录像占用(GB)。
    :param clips_gb: 成品占用(GB)。
    """
    notify(
        f"磁盘空间不足 ({free_gb:.1f}GB)",
        f"**磁盘剩余空间仅 {free_gb:.1f}GB**,低于阈值 {threshold_gb}GB。\n"
        f"- 原始录像: {raw_gb:.1f}GB\n"
        f"- 成品切片: {clips_gb:.1f}GB\n"
        f"- 建议清理旧文件或释放空间。",
        rule="notify_on_disk_alert",
    )


def notify_task_failed(task_id: int, stage: str, error: str) -> None:
    """任务永久失败通知。

    :param task_id: 任务 ID。
    :param stage: 失败阶段。
    :param error: 错误信息。
    """
    notify(
        f"任务失败 #{task_id}",
        f"**任务 #{task_id}** 在阶段 `{stage}` 永久失败。\n- 错误: {error}",
        rule="notify_on_error",
    )


def notify_upload_complete(clip_id: int, title: str, remote_id: str | None = None) -> None:
    """投稿完成通知。

    :param clip_id: 成品 ID。
    :param title: 视频标题。
    :param remote_id: 远程平台 ID(如 BV 号)。
    """
    extra = f"\n- 稿件号: {remote_id}" if remote_id else ""
    notify(
        f"投稿完成 #{clip_id}",
        f"**{title or '未命名切片'}** 已成功投稿。{extra}",
        rule="notify_on_upload",
    )
