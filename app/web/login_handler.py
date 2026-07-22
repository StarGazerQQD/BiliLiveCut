"""Bilibili 浏览器登录与 Cookie 自动收集（Playwright 实现）。

打开一个无预存登录态的浏览器窗口,用户手动扫码/手机号/密码登录后,
自动检测登录成功并提取 Cookie 持久化到运行时设置。

优先使用系统已安装的 Google Chrome；没有可用 Chrome 时自动安装并使用
Playwright 托管的 Chromium。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from loguru import logger

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Playwright


class LoginResult(TypedDict, total=False):
    """浏览器登录后台任务的可观察状态。"""

    status: str
    cookie: str
    error: str


_RUNNING_LOGINS: dict[int, LoginResult] = {}
_next_task_id = 1
_BROWSER_INSTALL_LOCK = threading.Lock()

# 登录完成所需的 cookie key
_LOGIN_MARKER = "DedeUserID"


def _playwright_environment() -> dict[str, str]:
    """返回浏览器进程环境，并将 Portable 浏览器固定到程序目录。"""
    if os.environ.get("BLC_PORTABLE") == "1" and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        browser_dir = (Path.cwd() / "vendor" / "playwright-browsers").resolve()
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_dir)
    return os.environ.copy()


def _install_playwright_chromium(result_store: LoginResult) -> None:
    """使用当前运行环境的 Python 安装 Playwright Chromium。"""
    result_store["status"] = "installing_browser"
    command = [sys.executable, "-m", "playwright", "install", "chromium"]
    logger.info("未找到可用的 Chrome，正在下载 Playwright Chromium。")
    with _BROWSER_INSTALL_LOCK:
        try:
            subprocess.run(command, check=True, timeout=900, env=_playwright_environment())
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Playwright Chromium 下载超过 15 分钟，请检查网络后重试。") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Playwright Chromium 下载失败（退出码 {exc.returncode}），请检查网络和磁盘空间后重试。"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"无法启动 Chromium 下载程序：{exc}") from exc
    result_store["status"] = "starting"


def _launch_login_context(
    playwright: Playwright,
    result_store: LoginResult,
    browser_error: type[Exception],
) -> BrowserContext:
    """优先启动系统 Chrome，不可用时安装并启动托管 Chromium。"""
    launch_options = {
        "user_data_dir": "",
        "headless": False,
        "args": ["--no-first-run", "--no-default-browser-check"],
        "viewport": {"width": 480, "height": 720},
        "locale": "zh-CN",
    }
    try:
        context = playwright.chromium.launch_persistent_context(channel="chrome", **launch_options)
        logger.info("使用系统 Google Chrome 打开 Bilibili 登录页。")
        return context
    except browser_error as exc:
        logger.info("系统 Google Chrome 不可用，将回退到 Playwright Chromium：{}", exc)

    managed_executable = Path(playwright.chromium.executable_path)
    if not managed_executable.is_file():
        _install_playwright_chromium(result_store)

    try:
        context = playwright.chromium.launch_persistent_context(**launch_options)
    except browser_error as exc:
        raise RuntimeError(f"Playwright Chromium 启动失败：{exc}") from exc
    logger.info("使用 Playwright Chromium 打开 Bilibili 登录页。")
    return context


def _extract_cookie_string(page) -> str:
    """从 Playwright page 提取 Bilibili 域下全部 Cookie,拼接为标准字符串。"""
    try:
        cookies = page.context.cookies([".bilibili.com", "live.bilibili.com"])
    except Exception:
        cookies = []
    if not cookies:
        return ""
    parts = [f"{c['name']}={c['value']}" for c in cookies]
    return "; ".join(parts)


def _save_cookie(cookie_string: str) -> None:
    """将 Cookie 持久化到运行时设置,供录制/弹幕模块使用。"""
    from app.core import settings_store

    settings_store.set_setting("bilibili_cookie", cookie_string)
    # 安全摘要: 计数 + 键名列表, 不输出原始值
    kv_count = cookie_string.count(";") + 1 if cookie_string else 0
    keys = []
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" in part:
            keys.append(part.split("=", 1)[0].strip())
    key_list = ", ".join(keys[:10])
    if len(keys) > 10:
        key_list += f" ... (+{len(keys) - 10})"
    logger.info("Bilibili Cookie saved: {} kv pairs, keys=[{}]", kv_count, key_list)


def _login_task(result_store: LoginResult) -> None:
    """在后台线程中执行浏览器登录流程。

    :param result_store: 外部注入的字典,用于回传结果。
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        result_store["error"] = (
            "当前运行环境缺少 Playwright。Portable 用户请执行 --repair；源码环境请安装 Web 依赖：pip install '.[web]'。"
        )
        return

    try:
        _playwright_environment()
        with sync_playwright() as p:
            context = _launch_login_context(p, result_store, PlaywrightError)
            page = context.pages[0]
            # 先清除所有 cookie（确保无残留）
            context.clear_cookies()

            result_store["status"] = "waiting"
            page.goto("https://passport.bilibili.com/login", timeout=30000, wait_until="domcontentloaded")
            logger.info("Bilibili 登录页已打开,请在浏览器中完成登录。")

            # 轮询检测登录成功:检查 cookie 中是否出现 DedeUserID
            logged_in = False
            deadline = time.time() + 120  # 最多等 2 分钟
            while time.time() < deadline:
                try:
                    cookies = context.cookies([".bilibili.com"])
                    for c in cookies:
                        if c["name"] == _LOGIN_MARKER and c["value"]:
                            logged_in = True
                            break
                except Exception:
                    pass
                if logged_in:
                    break
                time.sleep(1.5)

            if not logged_in:
                result_store["error"] = "登录超时（2 分钟）,未检测到 Bilibili 登录态。"
                context.close()
                return

            # 等待页面稳定,确保所有 cookie 写入完毕
            time.sleep(2)

            cookie_str = _extract_cookie_string(page)
            if cookie_str and _LOGIN_MARKER.lower() in cookie_str.lower():
                _save_cookie(cookie_str)
                result_store["status"] = "done"
                logger.info("Bilibili 登录成功,Cookie 已自动保存。")
            else:
                result_store["error"] = "Cookie 提取失败,请重试。"

            context.close()
    except RuntimeError as exc:
        result_store["error"] = str(exc)
    except Exception as exc:
        logger.exception("浏览器登录流程异常")
        result_store["error"] = f"未知错误: {exc}"


def start_login() -> dict:
    """启动一次浏览器登录流程,立即返回任务 ID 与初始状态。

    登录在后台线程中异步执行,前端通过 ``/api/login/status`` 轮询结果。

    :returns: ``{task_id, status}``。
    """
    global _next_task_id  # noqa: PLW0603
    task_id = _next_task_id
    _next_task_id += 1

    result: dict = {"status": "starting"}
    _RUNNING_LOGINS[task_id] = result

    thread = threading.Thread(target=_login_task, args=(result,), daemon=True)
    thread.start()
    return {"task_id": task_id, "status": "starting"}


def get_login_status(task_id: int) -> dict:
    """查询登录任务当前状态。

    :param task_id: ``start_login`` 返回的任务 id。
    :returns: ``{status, cookie?, error?, uid?}``。
    """
    result = _RUNNING_LOGINS.get(task_id)
    if result is None:
        return {"status": "not_found"}
    status = result.get("status", "unknown")
    resp: dict = {"status": status}
    if "cookie" in result:
        resp["cookie_available"] = True  # 仅告知前端 cookie 已就绪,不暴露完整值
    if "error" in result:
        resp["error"] = result["error"]
    # 清理已完成/失败的任务
    if status in ("done",) or "error" in result:
        _RUNNING_LOGINS.pop(task_id, None)
    return resp


def get_cookie_info() -> dict:
    """获取当前已保存的 Cookie 摘要信息（不暴露完整值）。

    :returns: ``{has_cookie, uid?, hint?}``。
    """
    from app.core import settings_store

    raw = settings_store.get_setting("bilibili_cookie", "")

    # Also check .env cookie (compat)
    if not raw:
        from app.core.config import settings

        raw = settings.bilibili_cookie

    # 安全摘要: 只输出 DedeUserID 值
    if not raw:
        return {"has_cookie": False}

    match = re.search(r"DedeUserID=(\d+)", raw)
    uid = match.group(1) if match else None

    return {
        "has_cookie": True,
        "uid": uid,
    }
