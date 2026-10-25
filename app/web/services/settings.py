"""Settings."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from loguru import logger

from app.core import settings_store
from app.core.config import get_settings, settings
from app.core.paths import clips_dir, ready_to_upload_dir
from app.core.sanitize import safe_exception_summary
from app.models.contracts import ModelProbe, ModelProbeResult
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
    overrides = settings_store.saved_keys(
        {"recording_pipeline_enabled", "transcript_llm_refine_enabled", "asr_task_max_concurrency"}
    )
    launcher_config = load_launcher_config(runtime_app_root(), warn=logger.warning)
    running_port = current_web_port()
    return {
        "recording_pipeline_enabled": settings_store.recording_pipeline_enabled(),
        "recording_pipeline_env_default": get_settings().recording_pipeline_enabled,
        "recording_pipeline_overridden": "recording_pipeline_enabled" in overrides,
        "transcript_llm_refine_enabled": settings_store.transcript_llm_refine_enabled(),
        "transcript_llm_refine_env_default": get_settings().transcript_llm_refine_enabled,
        "transcript_llm_refine_overridden": "transcript_llm_refine_enabled" in overrides,
        "asr_task_max_concurrency": settings_store.asr_task_max_concurrency(),
        "asr_task_max_concurrency_env_default": get_settings().asr_task_max_concurrency,
        "asr_task_max_concurrency_overridden": "asr_task_max_concurrency" in overrides,
        "web_port": launcher_config.web_port,
        "current_web_port": running_port,
        "restart_required": launcher_config.web_port != running_port,
        "biliup_enabled": settings_store.biliup_enabled(),
        "auto_upload": settings_store.auto_upload_enabled(),
        "upload_active": settings_store.upload_active(),
        "biliup_cmd_configured": bool(settings.biliup_upload_cmd.strip()),
        "clips_dir": str(clips_dir()),
        "ready_dir": str(ready_to_upload_dir()),
    }


def update_web_port(port: int) -> dict[str, Any]:
    """校验并保存启动端口，业务设置统一由配置中心处理。"""
    save_launcher_config(runtime_app_root(), web_port=validate_web_port(port))
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
    """逐个测试当前草稿或已保存 provider 的最终正文，使用有界的思考预算。

    :param items: 当前表单配置；为 ``None`` 时读取已保存配置。
    :returns: ``{"results": [{"id","name","ok","detail"}, ...]}``。
    """
    from app.analysis import llm as llm_mod
    from app.analysis import llm_providers as provs

    def _probe(p: provs.LLMProvider) -> dict[str, Any]:
        try:
            text = llm_mod.probe_provider(p)
            if not text.strip():
                raise llm_mod.EmptyLLMResponseError("服务已响应，但未返回可用正文")
            return {"id": p.id, "name": p.name, "ok": True, "detail": text[:40]}
        except Exception as exc:  # noqa: BLE001 — 汇总每个 provider 的错误
            from loguru import logger

            logger.opt(exception=exc).warning("LLM 连通测试失败 provider={} model={}", p.id, p.model)
            return {"id": p.id, "name": p.name, "ok": False, "detail": safe_exception_summary(exc)}

    if items is None:
        candidates = provs.active_providers()
    else:
        from app.models.configuration import authorize_model_probe, model_configuration_view

        revision = model_configuration_view().revision
        candidates = provs.merge_providers(items)
        for provider in candidates:
            provider.authorize = partial(authorize_model_probe, revision)
    providers = [p for p in candidates if p.enabled and p.api_key and p.base_url]
    results = await asyncio.to_thread(lambda: [_probe(p) for p in providers])
    return {"results": results}


def test_model_route(change: ModelProbe) -> ModelProbeResult:
    """按完整草稿解析一次文本调用，远程 ASR 需由用户明确提交音频测试。"""
    from openai import OpenAIError

    from app.analysis.llm import EmptyLLMResponseError, probe_provider
    from app.analysis.llm_providers import from_route
    from app.models.configuration import (
        ModelAuthorizationError,
        authorize_model_probe,
        probe_configuration,
        resolve_routes,
    )

    config = probe_configuration(change)
    route = next((item for item in config.routes if item.id == change.route_id), None)
    if route is None:
        raise ValueError("测试路由不存在")
    if route.capability == "asr":
        raise ValueError("远程转写请先保存并选择路由，再用短录播测试；连通测试不会自动上传音频")
    selected = next((item for item in resolve_routes(config, route.capability) if item[0].id == route.id), None)
    if selected is None:
        raise ValueError("测试路由或服务连接已停用")
    if settings.llm_daily_budget > 0:
        from app.analysis.llm import _today_spend

        if _today_spend() >= settings.llm_daily_budget:
            raise ValueError("已达到每日模型预算，无法发起测试请求")
    try:
        provider = from_route(*selected)
        provider.authorize = partial(authorize_model_probe, change.revision)
        result = probe_provider(provider)
    except (OpenAIError, EmptyLLMResponseError, ModelAuthorizationError) as exc:
        return ModelProbeResult(id=route.id, ok=False, detail=safe_exception_summary(exc))
    return ModelProbeResult(id=route.id, ok=True, detail=result[:80])
