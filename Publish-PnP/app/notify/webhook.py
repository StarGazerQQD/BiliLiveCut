"""多通道通知/Webhook 模块(V0.1.8 P2)。

支持:
- 钉钉机器人 Webhook(含加签)
- 企业微信机器人 Webhook
- SMTP 邮件

典型调用点:
- 切片完成(clipper.py produce_clip 末尾)
- 磁盘告警(monitor 阈值触发)
- 任务永久失败(task worker)
"""

from __future__ import annotations

import hashlib
import hmac
import smtplib
import time
import urllib.parse
from datetime import UTC, datetime
from email.mime.text import MIMEText

from loguru import logger

from app.core.config import settings


# 允许的 webhook 域名白名单。
_ALLOWED_WEBHOOK_DOMAINS = {"oapi.dingtalk.com", "qyapi.weixin.qq.com"}


def _validate_webhook(url: str, name: str = "") -> bool:
    """校验 webhook URL 域名是否在白名单内。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in _ALLOWED_WEBHOOK_DOMAINS:
        logger.warning("{}webhook 域名不在白名单: {}", name, parsed.hostname)
        return False
    return True


def _enabled() -> bool:
    """检查是否有任一通知通道已配置。"""
    return bool(settings.notify_enabled and (
        settings.dingtalk_webhook
        or settings.wecom_webhook
        or (settings.smtp_host and settings.smtp_user and settings.smtp_to)
    ))


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
    if settings.dingtalk_secret:
        ts, sign = _dingtalk_sign(settings.dingtalk_secret)
        webhook = f"{settings.dingtalk_webhook}?timestamp={ts}&sign={sign}"

    import httpx

    if not _validate_webhook(webhook, "钉钉"):
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title[:64],
            "text": f"## {title}\n\n{text}\n\n> BiliLiveCut {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC",
        },
    }
    try:
        resp = httpx.post(webhook, json=payload, timeout=10)
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info("钉钉通知已发送: {}", title)
            return True
        logger.warning("钉钉通知失败: {}", result)
        return False
    except Exception as exc:
        logger.error("钉钉通知异常: {}", exc)
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
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info("企业微信通知已发送: {}", title)
            return True
        logger.warning("企业微信通知失败: {}", result)
        return False
    except Exception as exc:
        logger.error("企业微信通知异常: {}", exc)
        return False


# --------------------------------------------------------------------------- #
# 邮件通知
# --------------------------------------------------------------------------- #


def send_email(subject: str, body: str) -> bool:
    """通过 SMTP 发送邮件通知。

    :param subject: 邮件主题。
    :param body: HTML 正文。
    :returns: 是否成功。
    """
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_to):
        return False

    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = settings.smtp_to
    msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")

    try:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10)
        try:
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        finally:
            server.quit()
        logger.info("邮件通知已发送: {}", subject)
        return True
    except Exception as exc:
        logger.error("邮件通知失败: {}", exc)
        return False


# --------------------------------------------------------------------------- #
# 统一通知入口
# --------------------------------------------------------------------------- #


def notify(title: str, body: str) -> None:
    """通过所有已配置通道发送通知。

    :param title: 通知标题。
    :param body: 通知正文(纯文本/Markdown)。
    """
    if not _enabled():
        return
    send_dingtalk(title, body)
    send_wecom(title, body)
    send_email(title, f"<h2>{title}</h2><pre>{body}</pre>")


def notify_clip_complete(candidate_id: int, clip_path: str, duration_s: float) -> None:
    """切片完成通知。

    :param candidate_id: 候选 ID。
    :param clip_path: 成品文件路径。
    :param duration_s: 时长(秒)。
    """
    if not settings.notify_on_clip:
        return
    notify(
        f"切片完成 #cand{candidate_id}",
        f"**候选 #{candidate_id}** 已生成成品切片。\n"
        f"- 时长: {duration_s:.0f}s\n"
        f"- 路径: `{clip_path}`",
    )


def notify_disk_alert(free_gb: float, threshold_gb: int, raw_gb: float, clips_gb: float) -> None:
    """磁盘空间不足告警。

    :param free_gb: 剩余空间(GB)。
    :param threshold_gb: 告警阈值(GB)。
    :param raw_gb: 原始录像占用(GB)。
    :param clips_gb: 成品占用(GB)。
    """
    if not settings.notify_on_disk_alert:
        return
    notify(
        f"磁盘空间不足 ({free_gb:.1f}GB)",
        f"**磁盘剩余空间仅 {free_gb:.1f}GB**,低于阈值 {threshold_gb}GB。\n"
        f"- 原始录像: {raw_gb:.1f}GB\n"
        f"- 成品切片: {clips_gb:.1f}GB\n"
        f"- 建议清理旧文件或释放空间。",
    )


def notify_task_failed(task_id: int, stage: str, error: str) -> None:
    """任务永久失败通知。

    :param task_id: 任务 ID。
    :param stage: 失败阶段。
    :param error: 错误信息。
    """
    if not settings.notify_on_error:
        return
    notify(
        f"任务失败 #{task_id}",
        f"**任务 #{task_id}** 在阶段 `{stage}` 永久失败。\n"
        f"- 错误: {error}",
    )
