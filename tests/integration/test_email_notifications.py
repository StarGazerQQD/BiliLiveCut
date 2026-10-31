"""真实配置/HTTP/数据库验证流程，邮件仅发送到测试替身。"""

from __future__ import annotations

import re
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from app.core.configuration import ConfigurationChange, save_configuration
from app.db.entities import AppSetting
from app.db.session import get_session
from app.notify import email_verification as mail
from app.notify.mail import MailError

if TYPE_CHECKING:
    from pytest import MonkeyPatch

    from app.notify.mail import MailConfig

BASE = "/api/settings/notifications/email"


@pytest.fixture
def email_setup(temp_db, monkeypatch):
    from app.web.main import app

    sent = []
    clock = [1_800_000_000.0]
    monkeypatch.setattr(mail, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(mail, "transmit", lambda config, subject, body, **kwargs: sent.append((config, subject, body)))
    save_configuration(
        ConfigurationChange(
            values={
                "smtp_host": "smtp.example.invalid",
                "smtp_port": 587,
                "smtp_security": "starttls",
                "smtp_user": "sender@example.invalid",
                "smtp_password": "private-mail-password",
                "smtp_to": "owner@example.invalid",
                "notify_enabled": True,
            }
        )
    )
    return TestClient(app, base_url="http://127.0.0.1"), sent, clock


def issue(client, sent):
    response = client.post(BASE + "/verification-code")
    assert response.status_code == 200, response.text
    code = re.search(r"验证码：([0-9]{6})", sent[-1][2]).group(1)
    assert code not in response.text
    assert "digest" not in response.text and "private-mail-password" not in response.text
    return response.json()["challenge_id"], code


def verify(client, sent):
    challenge, code = issue(client, sent)
    response = client.post(BASE + "/verify", json={"challenge_id": challenge, "code": code})
    assert response.status_code == 200, response.text
    assert response.json()["verified"] is True
    return challenge, code


def test_business_email_escapes_html_after_recipient_verification(
    email_setup: tuple[TestClient, list[tuple[MailConfig, str, str]], list[float]], monkeypatch: MonkeyPatch
) -> None:
    from app.notify import webhook

    client, sent, _clock = email_setup
    delivered = threading.Event()
    title, body = "<script>alert(1)</script>", '<img src="private" onerror="alert(1)">'

    def transmit(config: MailConfig, subject: str, content: str, **kwargs: object) -> None:
        sent.append((config, subject, content))
        if subject == title:
            delivered.set()

    monkeypatch.setattr(mail, "transmit", transmit)
    with client:
        verify(client, sent)
        webhook.notify(title, body)
        assert delivered.wait(3)
    html = sent[-1][2]
    assert "<script>" not in html and "<img" not in html
    assert "&lt;script&gt;" in html and "&lt;img" in html


def test_save_and_code_delivery_do_not_authorize_runtime_mail(email_setup):
    client, sent, _ = email_setup
    assert sent == []
    assert mail.send_verified_email("runtime", "body") is False
    challenge, code = issue(client, sent)
    assert mail.send_verified_email("runtime", "body") is False
    with get_session() as db:
        stored = db.get(AppSetting, "mail:verification").value
        assert f'"digest":"{code}"' not in stored
        assert '"verified_at":null' in stored
    result = client.post(BASE + "/verify", json={"challenge_id": challenge, "code": code})
    assert result.json()["can_notify"] is True
    assert mail.send_verified_email("runtime", "body") is True
    assert len(sent) == 2
    assert client.get(BASE).json()["delivery"]["ok"] is True
    assert client.post(BASE + "/verify", json={"challenge_id": challenge, "code": code}).status_code == 400


def test_wrong_codes_are_persisted_and_locked_out(email_setup):
    client, sent, _ = email_setup
    challenge, code = issue(client, sent)
    wrong = "000000" if code != "000000" else "111111"
    for attempt in range(5):
        assert client.post(BASE + "/verify", json={"challenge_id": challenge, "code": wrong}).status_code == 400
        assert client.get(BASE).json()["attempts_remaining"] == 4 - attempt
    assert client.post(BASE + "/verify", json={"challenge_id": challenge, "code": code}).status_code == 400
    assert mail.send_verified_email("runtime", "body") is False


def test_expiry_resend_and_global_hourly_limit(email_setup):
    client, sent, clock = email_setup
    old, code = issue(client, sent)
    assert client.post(BASE + "/verification-code").status_code == 409
    clock[0] += 601
    assert client.post(BASE + "/verify", json={"challenge_id": old, "code": code}).status_code == 400
    for _ in range(4):
        clock[0] += 61
        issue(client, sent)
    clock[0] += 61
    assert client.post(BASE + "/verification-code").status_code == 409
    assert len(sent) == 5


@pytest.mark.parametrize(
    "key,value",
    [
        ("smtp_to", "other@example.invalid"),
        ("smtp_from", "other@example.invalid"),
        ("smtp_host", "other.example.invalid"),
        ("smtp_port", 465),
        ("smtp_security", "ssl"),
        ("smtp_user", "other@example.invalid"),
        ("smtp_password", "new-password"),
    ],
)
def test_any_transport_change_revokes_permission_even_when_reverted(email_setup, key, value):
    client, sent, _ = email_setup
    verify(client, sent)
    original = {field["key"]: field["value"] for field in client.get("/api/settings/configuration").json()["fields"]}
    previous = "private-mail-password" if key == "smtp_password" else original[key]
    save_configuration(ConfigurationChange(values={key: value}))
    assert client.get(BASE).json()["verified"] is False
    save_configuration(ConfigurationChange(values={key: previous}))
    assert client.get(BASE).json()["verified"] is False
    assert mail.send_verified_email("runtime", "body") is False


def test_rule_changes_and_blank_secret_keep_verification_but_disabled_gate_blocks(email_setup):
    client, sent, _ = email_setup
    verify(client, sent)
    save_configuration(ConfigurationChange(values={"smtp_password": "", "notify_enabled": False}))
    assert client.get(BASE).json()["verified"] is True
    assert client.get(BASE).json()["can_notify"] is False
    assert mail.send_verified_email("runtime", "body") is False


def test_failed_smtp_and_config_change_during_delivery_cannot_verify(email_setup, monkeypatch):
    client, sent, clock = email_setup

    def failed(*args, **kwargs):
        raise MailError("SMTP 认证失败，请检查用户名和邮箱授权码")

    monkeypatch.setattr(mail, "transmit", failed)
    response = client.post(BASE + "/verification-code")
    assert response.status_code == 400
    assert client.get(BASE).json()["challenge_id"] is None
    clock[0] += 61

    def changed(*args, **kwargs):
        save_configuration(ConfigurationChange(values={"smtp_to": "changed@example.invalid"}))

    monkeypatch.setattr(mail, "transmit", changed)
    assert client.post(BASE + "/verification-code").status_code == 409
    assert client.get(BASE).json()["verified"] is False


def test_connection_alerts_deduplicate_persistently_and_respect_toggle(email_setup):
    client, sent, clock = email_setup
    verify(client, sent)
    assert mail.send_verified_email("WS", "body", alert_key="bilibili:1:expired") is True
    assert mail.send_verified_email("WS", "body", alert_key="bilibili:1:expired") is False
    assert mail.send_verified_email("WS", "body", alert_key="bilibili:2:expired") is True
    clock[0] += 1801
    assert mail.send_verified_email("WS", "body", alert_key="bilibili:1:expired") is True
    save_configuration(ConfigurationChange(values={"notify_on_connection": False}))
    assert mail.send_verified_email("WS", "body", alert_key="bilibili:3:expired") is False


def test_verification_routes_require_admin_and_same_origin(email_setup, monkeypatch):
    client, sent, _ = email_setup
    from app.web import main

    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.setattr(main, "_REVIEWER_PASSWORDS", {"reviewer": "review-pass"})
    assert client.post(BASE + "/verification-code").status_code == 401
    assert client.post(BASE + "/verification-code", auth=("reviewer", "review-pass")).status_code == 403
    assert (
        client.post(
            BASE + "/verification-code", auth=("admin", "admin-pass"), headers={"Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    assert sent == []
