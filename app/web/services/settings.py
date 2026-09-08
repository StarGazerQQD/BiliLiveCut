"""Settings."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from app.core import settings_store
from app.core.config import get_settings, settings
from app.core.paths import clips_dir, ready_to_upload_dir
from config.launcher_settings import (
    current_web_port,
    load_launcher_config,
    runtime_app_root,
    save_launcher_config,
    validate_web_port,
)


def get_settings_view() -> dict[str, Any]:
    """返回可在后台切换的运行时开关及只读上传配置。

    :returns: 设置视图字典。
    """
    pipeline_override = settings_store.get_setting("recording_pipeline_enabled", "").strip()
    refinement_override = settings_store.get_setting("transcript_llm_refine_enabled", "").strip()
    asr_concurrency_override = settings_store.get_setting("asr_task_max_concurrency", "").strip()
    launcher_config = load_launcher_config(runtime_app_root(), warn=logger.warning)
    running_port = current_web_port()
    return {
        "recording_pipeline_enabled": settings_store.recording_pipeline_enabled(),
        "recording_pipeline_env_default": get_settings().recording_pipeline_enabled,
        "recording_pipeline_overridden": bool(pipeline_override),
        "transcript_llm_refine_enabled": settings_store.transcript_llm_refine_enabled(),
        "transcript_llm_refine_env_default": get_settings().transcript_llm_refine_enabled,
        "transcript_llm_refine_overridden": bool(refinement_override),
        "asr_task_max_concurrency": settings_store.asr_task_max_concurrency(),
        "asr_task_max_concurrency_env_default": get_settings().asr_task_max_concurrency,
        "asr_task_max_concurrency_overridden": bool(asr_concurrency_override),
        "web_port": launcher_config.web_port,
        "current_web_port": running_port,
        "restart_required": launcher_config.web_port != running_port,
        "biliup_enabled": settings_store.biliup_enabled(),
        "auto_upload": settings_store.auto_upload_enabled(),
        "upload_active": settings_store.upload_active(),
        "biliup_cmd_configured": bool(settings.biliup_upload_cmd.strip()),
        "default_uploader": settings.uploader,
        "clips_dir": str(clips_dir()),
        "ready_dir": str(ready_to_upload_dir()),
    }


def update_settings(fields: dict[str, Any]) -> dict[str, Any]:
    """整体校验后保存；保留旧接口的端口与开关联合提交契约。"""
    from collections.abc import Iterator
    from contextlib import contextmanager

    from sqlalchemy.exc import SQLAlchemyError

    from app.core.configuration import ConfigurationChange, save_configuration

    values = {key: value for key, value in fields.items() if value is not None and key != "web_port"}
    port = validate_web_port(fields["web_port"]) if fields.get("web_port") is not None else None

    @contextmanager
    def launcher_change() -> Iterator[None]:
        if port is None:
            yield
            return
        previous = load_launcher_config(runtime_app_root(), warn=logger.warning).web_port
        save_launcher_config(runtime_app_root(), web_port=port)
        try:
            yield
        except (SQLAlchemyError, OSError, ValueError, RuntimeError):
            save_launcher_config(runtime_app_root(), web_port=previous)
            raise

    save_configuration(ConfigurationChange(values=values), external_change=launcher_change())
    return get_settings_view()


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


async def test_llm_providers(items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """逐个测试当前表单或已保存 provider 的连通性(各发一次极小请求)。

    :param items: 当前表单配置；为 ``None`` 时读取已保存配置。
    :returns: ``{"results": [{"id","name","ok","detail"}, ...]}``。
    """
    from app.analysis import llm as llm_mod
    from app.analysis import llm_providers as provs

    def _probe(p: provs.LLMProvider) -> dict[str, Any]:
        try:
            text = llm_mod._complete(p, "只回复 pong", max_tokens=64)
            if not text.strip():
                raise llm_mod.EmptyLLMResponseError("服务已响应，但未返回可用正文")
            return {"id": p.id, "name": p.name, "ok": True, "detail": text[:40]}
        except Exception as exc:  # noqa: BLE001 — 汇总每个 provider 的错误
            return {"id": p.id, "name": p.name, "ok": False, "detail": str(exc)[:200]}

    candidates = provs.active_providers() if items is None else provs.merge_providers(items)
    providers = [p for p in candidates if p.enabled and p.api_key and p.base_url]
    results = await asyncio.to_thread(lambda: [_probe(p) for p in providers])
    return {"results": results}
