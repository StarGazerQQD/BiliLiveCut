"""运行时设置存储(可在 Web 后台动态切换、跨重启持久化)。

与 :mod:`app.core.config`(只读、来自 ``.env``)互补:此处存放需要用户在界面上
随时切换的开关,持久化在 ``app_settings`` 表。最典型的是 **biliup 上传开关**——
默认关闭,由用户自行决定是否启用。
"""

from __future__ import annotations

from app.db.models import AppSetting
from app.db.session import get_session

# 运行时开关默认值(全部保守:不自动、不启用 biliup)。
_DEFAULTS: dict[str, str] = {
    "biliup_enabled": "false",   # 是否启用 biliup 上传(你要求的开关,默认关闭)
    "auto_upload": "false",      # 成品 ready 后是否自动入队上传
    # 网感资料库定时采集(默认关闭;录制/分析进行时自动暂停)。
    "trend_schedule_enabled": "false",   # 是否启用每日定时采集
    "trend_schedule_start": "03:00",     # 每日采集窗口起(HH:MM,本地时间)
    "trend_schedule_end": "05:00",       # 每日采集窗口止(HH:MM,本地时间)
    "trend_schedule_interval_min": "30", # 窗口内每隔多少分钟迭代采集一次
}

_TRUE = {"1", "true", "yes", "on"}


def get_setting(key: str, default: str | None = None) -> str:
    """读取一个运行时设置值。

    :param key: 设置键。
    :param default: 缺省值;为 ``None`` 时回退到内置默认。
    :returns: 字符串值。
    """
    with get_session() as db:
        row = db.get(AppSetting, key)
        if row is not None:
            return row.value
    if default is not None:
        return default
    return _DEFAULTS.get(key, "")


def set_setting(key: str, value: str) -> None:
    """写入(或更新)一个运行时设置值。

    :param key: 设置键。
    :param value: 设置值(字符串)。
    """
    from app.db.models import utcnow

    with get_session() as db:
        row = db.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=value)
        else:
            row.value = value
            row.updated_at = utcnow()
        db.add(row)


def get_bool(key: str) -> bool:
    """以布尔语义读取设置。

    :param key: 设置键。
    :returns: 布尔值。
    """
    return get_setting(key).strip().lower() in _TRUE


def set_bool(key: str, value: bool) -> None:
    """以布尔语义写入设置。

    :param key: 设置键。
    :param value: 布尔值。
    """
    set_setting(key, "true" if value else "false")


def biliup_enabled() -> bool:
    """biliup 上传开关是否开启(默认关闭)。

    :returns: 开启返回 ``True``。
    """
    return get_bool("biliup_enabled")


def auto_upload_enabled() -> bool:
    """是否在成品 ready 后自动入队上传。

    :returns: 开启返回 ``True``。
    """
    return get_bool("auto_upload")


def upload_active() -> bool:
    """"上传模块"是否处于启用状态。

    定义:启用了 biliup 开关即视为上传模块开启;否则为关闭(走人工/弹目录)。

    :returns: 上传模块启用返回 ``True``。
    """
    return biliup_enabled()


def all_settings() -> dict[str, str]:
    """返回所有运行时设置(含未覆盖项的默认值)。

    :returns: 键值字典。
    """
    result = dict(_DEFAULTS)
    with get_session() as db:
        from sqlmodel import select

        for row in db.exec(select(AppSetting)).all():
            result[row.key] = row.value
    return result
