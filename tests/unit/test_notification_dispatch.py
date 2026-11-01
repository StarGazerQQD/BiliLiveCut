from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import httpx
import pytest

from app.core.configuration import ConfigurationChange, save_configuration
from app.notify import delivery, webhook

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.mark.parametrize(
    "url",
    [
        "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=private",
        "https://user:private@qyapi.weixin.qq.com/cgi-bin/webhook/send",
        "https://qyapi.weixin.qq.com:8080/cgi-bin/webhook/send",
        "https://qyapi.weixin.qq.com:bad/cgi-bin/webhook/send",
        "https://[",
    ],
)
def test_invalid_webhook_does_not_reach_http(url: str, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(webhook.settings, "wecom_webhook", url)

    def forbidden(*args: object, **kwargs: object) -> httpx.Response:
        pytest.fail("无效 Webhook 不应发起 HTTP 请求")

    monkeypatch.setattr(httpx, "post", forbidden)
    assert webhook.send_wecom("title", "body") is False


def test_invalid_signed_dingtalk_url_is_rejected_before_signing(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(webhook.settings, "dingtalk_webhook", "https://[")
    monkeypatch.setattr(webhook.settings, "dingtalk_secret", "private-secret")
    assert webhook.send_dingtalk("title", "body") is False


@pytest.mark.parametrize("disabled", ["notify_enabled", "notify_on_error"])
async def test_queued_notification_rechecks_permissions(temp_db: None, monkeypatch: MonkeyPatch, disabled: str) -> None:
    save_configuration(
        ConfigurationChange(
            values={
                "notify_enabled": True,
                "notify_on_error": True,
                "wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=first",
            }
        )
    )
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def post(url: str, **kwargs: object) -> httpx.Response:
        calls.append(url)
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={"errcode": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    await delivery.start_email_delivery()
    try:
        webhook.notify_task_failed(1, "analyzing", "first")
        assert await asyncio.to_thread(entered.wait, 2)
        webhook.notify_task_failed(2, "analyzing", "second")
        save_configuration(ConfigurationChange(values={disabled: False}))
    finally:
        release.set()

        async def drained() -> None:
            while delivery._pending:
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(drained(), 3)
        finally:
            await delivery.stop_email_delivery()
    assert len(calls) == 1
