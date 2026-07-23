"""浏览器登录的 Chrome 优先与 Chromium 下载回退测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.web import login_handler

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


class FakeBrowserError(Exception):
    """模拟 Playwright 浏览器启动异常。"""


class FakeChromium:
    """只实现登录启动逻辑所需的 Chromium 接口。"""

    def __init__(self, executable: Path, *, chrome_available: bool) -> None:
        self.executable_path = str(executable)
        self.chrome_available = chrome_available
        self.calls: list[str | None] = []
        self.launch_options: list[dict[str, object]] = []
        self.context = object()

    def launch_persistent_context(self, **options: object) -> object:
        channel = options.get("channel")
        assert channel is None or isinstance(channel, str)
        self.calls.append(channel)
        self.launch_options.append(options)
        if channel == "chrome" and not self.chrome_available:
            raise FakeBrowserError("Chrome executable does not exist")
        if channel is None and not Path(self.executable_path).is_file():
            raise FakeBrowserError("Chromium executable does not exist")
        return self.context


class FakePlaywright:
    """提供测试所需的 Chromium browser type。"""

    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium


def test_launch_prefers_installed_chrome(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    chromium = FakeChromium(tmp_path / "missing-chromium.exe", chrome_available=True)

    def fail_install(_result_store: login_handler.LoginResult) -> None:
        pytest.fail("系统 Chrome 可用时不应下载 Chromium")

    monkeypatch.setattr(login_handler, "_install_playwright_chromium", fail_install)

    context = login_handler._launch_login_context(FakePlaywright(chromium), {}, FakeBrowserError)

    assert context is chromium.context
    assert chromium.calls == ["chrome"]
    launch_options = chromium.launch_options[0]
    assert launch_options["chromium_sandbox"] is True
    assert "--no-sandbox" not in launch_options["args"]
    assert launch_options["user_data_dir"] == ""


def test_launch_reuses_existing_chromium_when_chrome_is_missing(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    executable = tmp_path / "chromium.exe"
    executable.touch()
    chromium = FakeChromium(executable, chrome_available=False)

    def fail_install(_result_store: login_handler.LoginResult) -> None:
        pytest.fail("已有托管 Chromium 时不应重复下载")

    monkeypatch.setattr(login_handler, "_install_playwright_chromium", fail_install)

    context = login_handler._launch_login_context(FakePlaywright(chromium), {}, FakeBrowserError)

    assert context is chromium.context
    assert chromium.calls == ["chrome", None]


def test_launch_downloads_chromium_when_no_browser_exists(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    executable = tmp_path / "chromium.exe"
    chromium = FakeChromium(executable, chrome_available=False)
    result: login_handler.LoginResult = {"status": "starting"}
    install_calls = 0

    def fake_install(result_store: login_handler.LoginResult) -> None:
        nonlocal install_calls
        install_calls += 1
        result_store["status"] = "installing_browser"
        executable.touch()
        result_store["status"] = "starting"

    monkeypatch.setattr(login_handler, "_install_playwright_chromium", fake_install)

    context = login_handler._launch_login_context(FakePlaywright(chromium), result, FakeBrowserError)

    assert context is chromium.context
    assert chromium.calls == ["chrome", None]
    assert install_calls == 1
    assert result["status"] == "starting"


def test_install_chromium_uses_current_python_without_dry_run(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["options"] = options
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BLC_PORTABLE", "1")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(login_handler.subprocess, "run", fake_run)
    result: login_handler.LoginResult = {"status": "starting"}

    login_handler._install_playwright_chromium(result)

    assert captured["command"] == [login_handler.sys.executable, "-m", "playwright", "install", "chromium"]
    assert "--dry-run" not in captured["command"]
    options = captured["options"]
    assert isinstance(options, dict)
    assert options["check"] is True
    assert options["timeout"] == 900
    env = options["env"]
    assert isinstance(env, dict)
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str((tmp_path / "vendor" / "playwright-browsers").resolve())
    assert result["status"] == "starting"


def test_install_chromium_reports_download_failure(monkeypatch: MonkeyPatch) -> None:
    def fail_run(_command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(7, "playwright install chromium")

    monkeypatch.setattr(login_handler.subprocess, "run", fail_run)

    with pytest.raises(RuntimeError, match="退出码 7"):
        login_handler._install_playwright_chromium({"status": "starting"})


def test_non_portable_environment_preserves_playwright_browser_path(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("BLC_PORTABLE", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "D:/shared/playwright")

    env = login_handler._playwright_environment()

    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "D:/shared/playwright"


class FakeCookieContext:
    """提供 Cookie 读取所需的最小 BrowserContext 接口。"""

    def __init__(self, cookies: list[dict[str, str]]) -> None:
        self._cookies = cookies
        self.calls = 0

    def cookies(self) -> list[dict[str, str]]:
        self.calls += 1
        return self._cookies


def test_extract_cookie_reads_context_and_filters_domain_boundaries() -> None:
    context = FakeCookieContext(
        [
            {"name": "SESSDATA", "value": "session", "domain": ".bilibili.com"},
            {"name": "DedeUserID", "value": "123", "domain": "passport.bilibili.com"},
            {"name": "ignored", "value": "outside", "domain": "evilbilibili.com"},
            {"name": "ignored2", "value": "outside", "domain": "example.com"},
        ]
    )

    cookie_string = login_handler._extract_cookie_string(context)  # type: ignore[arg-type]

    assert context.calls == 1
    assert cookie_string == "SESSDATA=session; DedeUserID=123"


def test_cookie_read_failure_keeps_original_exception() -> None:
    class FailingCookieContext:
        def cookies(self) -> list[dict[str, str]]:
            raise OSError("context closed")

    with pytest.raises(RuntimeError, match="无法从登录浏览器读取 Cookie") as exc_info:
        login_handler._get_bilibili_cookies(FailingCookieContext())  # type: ignore[arg-type]

    assert isinstance(exc_info.value.__cause__, OSError)
