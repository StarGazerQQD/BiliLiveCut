"""大模型配置."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from app.web import service


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
    return service.list_llm_providers()


@router.put("/llm-providers")
def put_llm_providers(req: LLMProvidersRequest) -> dict[str, Any]:
    """保存多大模型配置(按优先级失败回退；未填 key 保留已保存值)。"""
    try:
        return service.save_llm_providers([p.model_dump() for p in req.providers])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/llm-providers/test")
async def test_llm_providers(req: LLMProvidersRequest | None = None) -> dict[str, Any]:
    """逐个测试当前表单或已保存大模型的连通性，不持久化草稿。"""
    items = None if req is None else [p.model_dump() for p in req.providers]
    return await service.test_llm_providers(items)
