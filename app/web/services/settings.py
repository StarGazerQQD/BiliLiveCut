"""Settings (V0.1.14.2)."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from app.core import settings_store
from app.core.config import settings
from app.core.paths import clips_dir, ready_to_upload_dir


def get_settings_view() -> dict[str, Any]:
    """返回可在后台切换的运行时开关及只读上传配置。

    :returns: 设置视图字典。
    """
    return {
        "biliup_enabled": settings_store.biliup_enabled(),
        "auto_upload": settings_store.auto_upload_enabled(),
        "upload_active": settings_store.upload_active(),
        "biliup_cmd_configured": bool(settings.biliup_upload_cmd.strip()),
        "default_uploader": settings.uploader,
        "clips_dir": str(clips_dir()),
        "ready_dir": str(ready_to_upload_dir()),
    }


def update_settings(fields: dict[str, Any]) -> dict[str, Any]:
    """更新运行时开关(biliup_enabled / auto_upload)。

    :param fields: 待更新开关。
    :returns: 更新后的设置视图。
    """
    if "biliup_enabled" in fields and fields["biliup_enabled"] is not None:
        settings_store.set_bool("biliup_enabled", bool(fields["biliup_enabled"]))
        logger.warning("biliup 上传开关被设置为 {}(合规风险自负)。", bool(fields["biliup_enabled"]))
    if "auto_upload" in fields and fields["auto_upload"] is not None:
        settings_store.set_bool("auto_upload", bool(fields["auto_upload"]))
    _update_trend_schedule(fields)
    return get_settings_view()


def _valid_hhmm(value: str) -> bool:
    """校验 ``HH:MM`` 时间字符串是否合法。

    :param value: 时间字符串。
    :returns: 合法返回 ``True``。
    """
    try:
        h, m = value.strip().split(":")
        return 0 <= int(h) < 24 and 0 <= int(m) < 60
    except (ValueError, AttributeError):
        return False


def _update_trend_schedule(fields: dict[str, Any]) -> None:
    """更新网感定时采集的相关设置(开关/窗口/间隔)。

    :param fields: 待更新字段。
    :raises ValueError: 时间格式或间隔非法时。
    """
    if fields.get("trend_schedule_enabled") is not None:
        settings_store.set_bool("trend_schedule_enabled", bool(fields["trend_schedule_enabled"]))
    for key in ("trend_schedule_start", "trend_schedule_end"):
        if fields.get(key) is not None:
            if not _valid_hhmm(str(fields[key])):
                raise ValueError(f"时间格式应为 HH:MM: {fields[key]}")
            settings_store.set_setting(key, str(fields[key]).strip())
    if fields.get("trend_schedule_interval_min") is not None:
        interval = int(fields["trend_schedule_interval_min"])
        if interval < 1:
            raise ValueError("采集间隔需 >= 1 分钟。")
        settings_store.set_setting("trend_schedule_interval_min", str(interval))


def list_llm_providers() -> dict[str, Any]:
    """返回多大模型配置(对外视图,key 掩码)。

    :returns: ``{"providers": [...], "active_count": N}``。
    """
    from app.analysis import llm_providers as provs

    return {
        "providers": provs.public_view(),
        "active_count": len(provs.active_providers()),
    }


def save_llm_providers(items: list[dict[str, Any]]) -> dict[str, Any]:
    """保存多大模型配置(未提供新 key 的条目沿用旧 key)。

    :param items: 前端提交的 provider 字典列表。
    :returns: 保存后的对外视图。
    """
    from app.analysis import llm_providers as provs

    provs.merge_and_save(items)
    return list_llm_providers()


async def test_llm_providers() -> dict[str, Any]:
    """逐个测试已启用 provider 的连通性(各发一次极小请求)。

    :returns: ``{"results": [{"id","name","ok","detail"}, ...]}``。
    """
    from app.analysis import llm as llm_mod
    from app.analysis import llm_providers as provs

    def _probe(p: provs.LLMProvider) -> dict[str, Any]:
        try:
            text = llm_mod._complete(p, "ping", max_tokens=1)
            return {"id": p.id, "name": p.name, "ok": True, "detail": (text or "")[:40]}
        except Exception as exc:  # noqa: BLE001 — 汇总每个 provider 的错误
            return {"id": p.id, "name": p.name, "ok": False, "detail": str(exc)[:200]}

    providers = provs.active_providers()
    results = await asyncio.to_thread(lambda: [_probe(p) for p in providers])
    return {"results": results}
