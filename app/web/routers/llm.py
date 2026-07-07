"""大模型配置 (V0.1.14.1)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.web import service


class LLMProviderIn(BaseModel):
    id: str = ""
    name: str = ""
    base_url: str
    model: str
    api_key: str = ""
    web_search_param: str = ""
    price_input_per_m: float = 0.0
    price_output_per_m: float = 0.0
    enabled: bool = True
    priority: int = 100

class LLMProvidersRequest(BaseModel):
    providers: list[LLMProviderIn]


router = APIRouter()

@router.get("/llm-providers")
def get_llm_providers() -> dict[str, Any]:
    """返回多大模型配置(key 掩码)与可用数量。"""
    return service.list_llm_providers()


@router.put("/llm-providers")
def put_llm_providers(req: LLMProvidersRequest) -> dict[str, Any]:
    """保存多大模型配置(按优先级失败回退;未填 key 沿用旧值)。"""
    return service.save_llm_providers([p.model_dump() for p in req.providers])


@router.post("/llm-providers/test")
async def test_llm_providers() -> dict[str, Any]:
    """逐个测试已启用大模型的连通性。"""
    return await service.test_llm_providers()

