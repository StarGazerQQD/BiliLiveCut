"""邮件与连接提醒的有界后台投递，不占连接或业务事务。"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from html import escape
from threading import RLock
from typing import Literal
from uuid import uuid4

from loguru import logger

from app.core.execution import run_sync
from app.core.runtime_settings import process_settings, settings_scope
from app.notify.email_verification import send_verified_email

_executor: ThreadPoolExecutor | None = None
_lock = RLock()
_pending: set[str] = set()
_accepting = True
NotificationRule = Literal["notify_on_clip", "notify_on_disk_alert", "notify_on_error", "notify_on_upload"]
NotificationChannel = Literal["dingtalk", "wecom", "lark", "email"]
_REASONS = {
    "cookie_expired": ("Bilibili 登录态已失效", "平台明确拒绝当前 Cookie。请到设置中心的账号登录页重新扫码登录。"),
    "login_rejected": ("Bilibili 弹幕登录失败", "登录访问失败，采集器正在按策略回退或重试。请检查账号登录状态。"),
    "ws_failed": ("Bilibili WS 连续连接失败", "连续三次连接失败。请检查网络、平台访问限制和账号登录状态。"),
}


def publish_email(subject: str, body: str, *, html: bool = False, alert_key: str | None = None) -> bool:
    """只入队；工作线程重新检查当前配置与验证许可，不继承任务旧快照。"""
    config = process_settings()
    if not config.notify_enabled or not config.smtp_host or not config.smtp_to:
        return False
    key = alert_key or uuid4().hex
    return _enqueue(key, partial(send_verified_email, subject, body, html=html, alert_key=alert_key))


def publish_notification(title: str, body: str, *, rule: NotificationRule | None = None) -> bool:
    """把业务通知投递给现有有界队列，消息不持有旧配置或凭据。"""
    config = process_settings()
    if not config.notify_enabled or (rule is not None and not getattr(config, rule)):
        return False
    channels: list[NotificationChannel] = []
    if config.dingtalk_webhook:
        channels.append("dingtalk")
    if config.wecom_webhook:
        channels.append("wecom")
    if config.lark_app_id and config.lark_app_secret and config.lark_receive_id:
        channels.append("lark")
    if config.smtp_host and config.smtp_to:
        channels.append("email")
    accepted = False
    identity = uuid4().hex
    for channel in channels:
        accepted = (
            _enqueue(f"notification:{identity}:{channel}", partial(_deliver_notification, channel, title, body, rule))
            or accepted
        )
    return accepted


def _deliver_notification(channel: NotificationChannel, title: str, body: str, rule: NotificationRule | None) -> bool:
    from app.notify import webhook
    from app.notify.lark import send_lark

    with settings_scope(fresh=True):
        config = process_settings()
        if not config.notify_enabled or (rule is not None and not getattr(config, rule)):
            return False
        if channel == "dingtalk":
            return webhook.send_dingtalk(title, body)
        if channel == "wecom":
            return webhook.send_wecom(title, body)
        if channel == "lark":
            return send_lark(title, body)
        return send_verified_email(title, f"<h2>{escape(title)}</h2><pre>{escape(body)}</pre>", html=True)


def _enqueue(key: str, deliver: Callable[[], bool]) -> bool:
    global _executor
    with _lock:
        if not _accepting or key in _pending:
            return False
        if len(_pending) >= 32:
            logger.warning("运行提醒队列已满，本次保留业务日志，不影响录制")
            return False
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blc-notify")
        _pending.add(key)
        future = _executor.submit(deliver)

    def finished(result: Future[bool]) -> None:
        with _lock:
            _pending.discard(key)
        if not result.cancelled() and result.exception() is not None:
            logger.warning("运行提醒处理失败，请检查通知配置及本地存储")

    future.add_done_callback(finished)
    return True


def publish_lark(title: str, body: str, *, alert_key: str | None = None) -> bool:
    """飞书 HTTP 调用也交给有界线程，不能阻塞录制和 WS 接收。"""
    from app.notify.lark import configured, send_connection_alert, send_lark

    if not process_settings().notify_enabled or not configured():
        return False

    def deliver() -> bool:
        if not process_settings().notify_enabled:
            return False
        return send_connection_alert(title, body, alert_key) if alert_key else send_lark(title, body)

    return _enqueue("lark:" + (alert_key or uuid4().hex), deliver)


def publish_connection_alert(room_id: int, reason: str) -> None:
    """按来源、房间和异常类型合并重复待发提醒，接收端负责持久冷却。"""
    if reason in _REASONS:
        title, detail = _REASONS[reason]
        body = f"直播间：{room_id}\n\n{detail}"
        key = f"bilibili:{room_id}:{reason}"
        publish_email(title, body, alert_key=key)
        if process_settings().notify_on_connection:
            publish_lark(title, body, alert_key=key)


async def start_email_delivery() -> None:
    """允许当前宿主投递，不创建业务管理器或提前建立 SMTP 连接。"""
    global _accepting
    with _lock:
        if not _accepting and _executor is not None:
            raise RuntimeError("通知投递线程尚未确认关闭，不能重新启动")
        _accepting = True


def request_stop_delivery() -> None:
    """立即拒绝新增提醒，正在发送的通知由清理执行等待。"""
    global _accepting
    with _lock:
        _accepting = False


async def stop_email_delivery() -> None:
    """不再接收提醒，取消未开始的邮件，等待正在发送的邮件完成。"""
    global _executor, _accepting
    with _lock:
        _accepting = False
        executor = _executor
    if executor is not None:
        await run_sync(partial(executor.shutdown, wait=True, cancel_futures=True))
        with _lock:
            if _executor is executor:
                _executor = None
