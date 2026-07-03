"""Bilibili Cookie 统一读取入口。

优先级:
1. 运行时设置（settings_store，即 Web 登录页采集的 Cookie）
2. .env 配置（config.settings.bilibili_cookie）
"""

from __future__ import annotations


def get_bilibili_cookie() -> str:
    """返回当前有效的 Bilibili Cookie 字符串。

    :returns: Cookie 字符串,未配置时为空串。
    """
    from app.core import settings_store

    cookie = settings_store.get_setting("bilibili_cookie", "")
    if cookie:
        return cookie

    from app.core.config import settings

    return settings.bilibili_cookie
