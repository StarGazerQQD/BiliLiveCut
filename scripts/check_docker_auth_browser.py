"""在真实 React 前端验证 Docker 初始化、改密、退出和失效会话的草稿保留。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Route, expect, sync_playwright


def check_browser(
    url: str,
    code: str,
    password: str,
    expire_session: Callable[[], None],
    *,
    channel: str | None = None,
    executable_path: str | None = None,
    artifacts: Path | None = None,
) -> str:
    """通过用户界面验收真实会话认证，不注入 Basic 凭据或打印密码。"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, channel=channel, executable_path=executable_path or os.environ.get("BLC_CHROMIUM_PATH")
        )
        try:
            context = browser.new_context()
            csp_violations: list[str] = []

            def record_violation(_source: dict[str, object], directive: str) -> None:
                csp_violations.append(directive)

            context.expose_binding("__recordCspViolation", record_violation)
            context.add_init_script(
                "document.addEventListener('securitypolicyviolation', event => "
                "void globalThis.__recordCspViolation(event.effectiveDirective))"
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            response = page.goto(url + "/")
            assert response is not None and "script-src 'self'" in response.headers.get("content-security-policy", "")
            missing_asset = context.request.get(url + "/assets/missing-csp-regression.js")
            assert missing_asset.status == 404
            assert "immutable" not in missing_asset.headers.get("cache-control", "")
            expect(page.get_by_role("heading", name="设置你的工作台")).to_be_visible()
            if artifacts is not None:
                page.screenshot(path=str(artifacts / "docker-setup.png"), full_page=True)
            page.get_by_label("一次性初始化码").fill(code)
            page.get_by_label("设置管理员密码", exact=True).fill(password)
            page.get_by_role("button", name="初始化并登录").click()
            intro = page.get_by_role("dialog", name="初次见面", exact=True)
            expect(intro).to_be_visible()
            expect(page.get_by_role("heading", name="工作台概览", exact=True)).to_have_count(0)
            intro.get_by_role("button", name="稍后再说", exact=True).click()
            expect(page.get_by_role("heading", name="工作台概览", exact=True)).to_be_visible()

            page.goto(url + "/settings/configuration")
            draft = page.get_by_label("录制分段目标时长", exact=False)
            draft.fill("420")
            writes: list[str] = []
            page.on(
                "request",
                lambda request: (
                    writes.append(request.url)
                    if request.method == "PATCH" and request.url.endswith("/api/settings/configuration")
                    else None
                ),
            )

            def expire_before_write(route: Route) -> None:
                if route.request.method == "PATCH":
                    expire_session()
                route.continue_()

            # 请求已经发出后再使真实会话过期，避免主动通知先打开登录对话框。
            page.route("**/api/settings/configuration", expire_before_write)
            try:
                with page.expect_response(
                    lambda response: (
                        response.request.method == "PATCH" and response.url.endswith("/api/settings/configuration")
                    )
                ) as rejected:
                    page.get_by_role("button", name="保存修改", exact=True).click()
                assert rejected.value.status == 401
            finally:
                page.unroute("**/api/settings/configuration", expire_before_write)
            relogin = page.get_by_role("dialog", name="重新登录，继续当前工作")
            expect(relogin).to_be_visible()
            if artifacts is not None:
                page.screenshot(path=str(artifacts / "docker-relogin.png"), full_page=True)
            relogin.get_by_label("密码", exact=True).fill(password)
            relogin.get_by_role("button", name="进入工作台").click()
            expect(relogin).not_to_be_visible()
            expect(draft).to_have_value("420")
            assert len(writes) == 1, "重新登录不能自动重发失败的配置写入"
            assert page.evaluate("() => document.cookie.includes('blc_session')") is False
            stored = page.evaluate("() => [...Object.values(localStorage), ...Object.values(sessionStorage)]")
            assert all(password not in value for value in stored)

            # 保留通知主动发现失效的路径，未点击保存时不能产生配置写入。
            draft.fill("421")
            expire_session()
            expect(relogin).to_be_visible(timeout=20000)
            relogin.get_by_label("密码", exact=True).fill(password)
            relogin.get_by_role("button", name="进入工作台").click()
            expect(relogin).not_to_be_visible()
            expect(draft).to_have_value("421")
            assert len(writes) == 1, "主动重新登录不能提交尚未保存的草稿"
            page.get_by_role("button", name="放弃修改", exact=True).click()

            page.goto(url + "/settings")
            page.get_by_role("button", name="修改后台密码", exact=True).click()
            change = page.get_by_role("dialog", name="修改后台密码")
            changed = password + "-updated"
            change.get_by_label("当前密码", exact=False).fill(password)
            change.get_by_label("新密码", exact=False).fill(changed)
            change.get_by_role("button", name="保存修改", exact=True).click()
            expect(relogin).to_be_visible()
            relogin.get_by_label("密码", exact=True).fill(changed)
            relogin.get_by_role("button", name="进入工作台").click()
            expect(relogin).not_to_be_visible()

            # 跨标签页换发 Cookie 后，首次写入携带旧 CSRF 时应刷新状态并要求手动重试。
            result = context.request.post(
                url + "/auth/login", data={"username": "admin", "password": changed}, headers={"Origin": url}
            )
            assert result.status == 200

            def logout() -> None:
                page.locator("header").get_by_role("button", name="admin", exact=False).click()
                page.get_by_role("menuitem", name="退出登录", exact=True).click()

            logout()
            expect(page.get_by_text("登录状态已更新", exact=False)).to_be_visible()
            logout()
            expect(relogin).to_be_visible()
            relogin.get_by_label("密码", exact=True).fill(changed)
            relogin.get_by_role("button", name="进入工作台").click()
            expect(relogin).not_to_be_visible()
            page.reload()
            expect(page.get_by_role("heading", name="设置中心", exact=True)).to_be_visible()
            assert page.evaluate(
                "async () => { await navigator.serviceWorker.register('/media-auth.js'); "
                "await navigator.serviceWorker.ready; return true }"
            )
            assert not csp_violations, csp_violations
            assert not errors, errors
            return changed
        finally:
            browser.close()
