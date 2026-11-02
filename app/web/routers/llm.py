"""大模型配置."""

from __future__ import annotations

from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.configuration import ConfigurationConflict
from app.core.execution import run_sync
from app.models.configuration import model_configuration_view, save_model_configuration
from app.models.contracts import ModelConfigurationChange, ModelConfigurationView, ModelProbe, ModelProbeResult
from app.web.services import settings as settings_service


class LLMProviderIn(BaseModel):
    """大模型提供商配置项。"""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    name: str = ""
    base_url: str
    model: str
    api_key: str = ""
    clear_api_key: bool = False
    web_search_param: str = ""
    deepseek_search_enabled: bool = False
    price_input_per_m: float = 0.0
    price_output_per_m: float = 0.0
    enabled: bool = True
    priority: int = 100


class LLMProvidersRequest(BaseModel):
    """大模型提供商批量设置请求体。"""

    model_config = ConfigDict(extra="forbid")

    providers: list[LLMProviderIn]


router = APIRouter()


@router.get("/llm-providers")
def get_llm_providers() -> dict[str, Any]:
    """返回多大模型配置(key 掩码)与可用数量。"""
    return settings_service.list_llm_providers()


@router.put("/llm-providers")
def put_llm_providers(req: LLMProvidersRequest) -> dict[str, Any]:
    """保存多大模型配置(按优先级失败回退；未填 key 保留已保存值)。"""
    try:
        return settings_service.save_llm_providers([p.model_dump() for p in req.providers])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/llm-providers/test")
async def test_llm_providers(req: LLMProvidersRequest | None = None) -> dict[str, Any]:
    """逐个测试当前表单或已保存大模型的连通性，不持久化草稿。"""
    items = None if req is None else [p.model_dump() for p in req.providers]
    return await settings_service.test_llm_providers(items)


@router.get("/model-configuration")
def get_model_configuration() -> ModelConfigurationView:
    """返回共享连接与各业务路由的脱敏视图。"""
    return model_configuration_view()


@router.put("/model-configuration")
def put_model_configuration(req: ModelConfigurationChange) -> ModelConfigurationView:
    """在同一配置版本内保存全部连接与路由，不修改运行中任务的快照。"""
    try:
        return save_model_configuration(req)
    except ConfigurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValidationError:
        raise HTTPException(status_code=400, detail="模型配置校验失败，请检查字段和引用约束") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/model-configuration/test")
async def test_model_configuration(req: ModelProbe) -> ModelProbeResult:
    """仅测试指定文本路由，保留真实执行的取消与收尾约束。"""
    try:
        return await run_sync(partial(settings_service.test_model_route, req))
    except ConfigurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValidationError:
        raise HTTPException(status_code=400, detail="模型配置校验失败，请检查字段和引用约束") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
