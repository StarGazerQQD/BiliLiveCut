# ruff: noqa: D100
from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx

from app.analysis.llm_providers import LLMProvider, save_providers

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def install_editorial_service(
    monkeypatch: MonkeyPatch,
    responder: Callable[..., str | None],
    *,
    grounded: bool | None = None,
    grounding_reply: Callable[[dict[str, object]], str] | None = None,
) -> list[dict[str, object]]:
    """仅替换模型 HTTP 边界，真实执行 SDK、授权、预算、解析与业务验证。"""
    save_providers(
        [
            LLMProvider(
                id="editorial-fixture",
                name="测试模型",
                base_url="https://fixture.invalid/v1",
                api_key="fixture-key",
                model="fixture-model",
                price_input_per_m=1,
                price_output_per_m=1,
            )
        ]
    )
    requests: list[dict[str, object]] = []
    original_send = httpx.AsyncHTTPTransport.handle_async_request

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        if request.url.host != "fixture.invalid":
            return await original_send(_transport, request)
        assert request.method == "POST" and request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        requests.append(payload)
        data = json.loads(payload["messages"][1]["content"])
        if "candidate" in data and grounding_reply is not None:
            text = grounding_reply(data)
        elif "candidate" in data:
            assert grounded is not None, "测试必须明确指定事实核验服务的响应"
            quotes = [
                {"evidence_id": item["evidence_id"], "quote_id": item["quotes"][0]["quote_id"]}
                for item in data["evidence"]
                if item["quotes"]
            ][:16]
            text = json.dumps(
                {
                    "judgements": [
                        {
                            "field": field,
                            "supported": grounded,
                            "reason": "受控模型 HTTP 响应",
                            "quotes": quotes if grounded else [],
                        }
                        for field in ("title", "summary")
                    ]
                },
                ensure_ascii=False,
            )
        else:
            text = responder(
                payload["messages"][0]["content"] + "\n" + payload["messages"][1]["content"],
                max_tokens=payload["max_tokens"],
            )
        if text is None:
            return httpx.Response(503, json={"error": {"message": "fixture unavailable"}}, request=request)
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "model": "fixture-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 30},
            },
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    return requests
