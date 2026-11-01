from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from app.analysis import llm, llm_providers

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch

CompletionAPI = Callable[[list[SimpleNamespace]], list[dict[str, object]]]


def _provider(base_url: str, model: str) -> llm_providers.LLMProvider:
    return llm_providers.LLMProvider(id="protocol", name="protocol", base_url=base_url, model=model, api_key="test-key")


def _response(content: str | None, finish: str = "stop", reasoning: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content, reasoning_content=reasoning))
        ],
        usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=100, completion_tokens_details=SimpleNamespace(reasoning_tokens=80)
        ),
    )


@pytest.fixture
def completion_api(temp_db: None, monkeypatch: MonkeyPatch) -> CompletionAPI:
    def install(responses: list[SimpleNamespace]) -> list[dict[str, object]]:
        replies = iter(responses)
        calls: list[dict[str, object]] = []

        def create(**kwargs: object) -> SimpleNamespace:
            calls.append(kwargs)
            return next(replies)

        # 仅替代外部 Chat Completions 边界，保留正文提取、重试与计费调用链。
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        monkeypatch.setattr(llm, "_get_client", lambda _provider: client)
        return calls

    return install


@pytest.mark.parametrize(
    ("base_url", "model", "expected"),
    [
        ("https://api.deepseek.com/v1", "deepseek-v4-pro", {"thinking": {"type": "disabled"}}),
        ("https://api.deepseek.com", "deepseek-flash", {"thinking": {"type": "disabled"}}),
        ("https://api.minimax.cn/v1", "MiniMax-M3.1-Flash", {"reasoning_effort": "low"}),
        ("https://api.minimax.cn/v1", "MiniMax-M2.7", None),
        ("https://api.deepseek.com", "unverified-model", None),
        ("https://gateway.example/v1", "deepseek-v4-pro", None),
        ("https://api.deepseek.com.example/v1", "deepseek-flash", None),
        ("https://gateway.example/v1", "MiniMax-M3.1-Flash", None),
    ],
)
def test_probe_uses_verified_model_options_and_bounded_budget(
    completion_api: CompletionAPI, base_url: str, model: str, expected: dict[str, object] | None
) -> None:
    calls = completion_api([_response("pong")])
    assert llm.probe_provider(_provider(base_url, model)) == "pong"
    assert len(calls) == 1
    parameter = (
        "max_completion_tokens"
        if model == "MiniMax-M3.1-Flash" and base_url == "https://api.minimax.cn/v1"
        else "max_tokens"
    )
    assert calls[0][parameter] == 4096
    assert calls[0].get("extra_body") == expected


def test_minimax_retry_keeps_thinking_and_accounts_both_requests(
    completion_api: CompletionAPI, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    provider = _provider("https://api.minimax.cn/v1", "MiniMax-M3.1-Flash")
    provider.price_input_per_m = 1
    provider.price_output_per_m = 2
    budget = tmp_path / "budget.json"
    monkeypatch.setattr(llm, "_budget_file", lambda: budget)
    calls = completion_api([_response(None, "length", "private reasoning"), _response("最终正文")])
    original = {"enable_search": True}

    assert llm._complete(provider, "生成正文", 512, original) == "最终正文"
    assert len(calls) == 2
    assert calls[0]["extra_body"] == {"enable_search": True}
    assert calls[1]["extra_body"] == {"enable_search": True, "reasoning_effort": "low"}
    assert [call["max_completion_tokens"] for call in calls] == [512, 512]
    assert original == {"enable_search": True}
    assert json.loads(budget.read_text())["spend_usd"] == pytest.approx(0.00042)


def test_unknown_reasoning_model_does_not_receive_vendor_parameters(completion_api: CompletionAPI) -> None:
    calls = completion_api([_response(None, "stop", "private reasoning"), _response("正文")])
    provider = _provider("https://gateway.example/v1", "custom-reasoner")
    assert llm._complete(provider, "生成正文", 2048, {"enable_search": True}) == "正文"
    assert len(calls) == 2
    assert all(call["extra_body"] == {"enable_search": True} for call in calls)


@pytest.mark.parametrize(
    ("base_url", "model"),
    [("https://gateway.example/v1", "custom-reasoner"), ("https://api.minimax.cn/v1", "MiniMax-M3.1-Flash")],
)
def test_probe_reports_budget_exhaustion_without_repeating_same_request(
    completion_api: CompletionAPI, base_url: str, model: str
) -> None:
    calls = completion_api([_response(None, "length", "private reasoning")])
    with pytest.raises(llm.EmptyLLMResponseError, match="输出预算已耗尽.*max_tokens=4096") as error:
        llm.probe_provider(_provider(base_url, model))
    assert len(calls) == 1
    assert "private reasoning" not in str(error.value)


@pytest.mark.parametrize("finish", ["content_filter", "tool_calls"])
def test_empty_filtered_or_tool_response_is_not_retried(completion_api: CompletionAPI, finish: str) -> None:
    calls = completion_api([_response(None, finish, "private reasoning")])
    with pytest.raises(llm.EmptyLLMResponseError, match=f"finish_reason={finish}"):
        llm._complete(_provider("https://api.minimax.cn/v1", "MiniMax-M3.1-Flash"), "ping", 512)
    assert len(calls) == 1


def test_reasoning_only_retry_never_becomes_business_text(completion_api: CompletionAPI) -> None:
    calls = completion_api(
        [_response(None, "length", "private reasoning"), _response(None, "stop", "private reasoning")]
    )
    with pytest.raises(llm.EmptyLLMResponseError, match="连续两次返回空正文") as error:
        llm._complete(_provider("https://api.minimax.cn/v1", "MiniMax-M3.1-Flash"), "ping", 512)
    assert len(calls) == 2
    assert "private reasoning" not in str(error.value)


def test_draft_probe_reaches_completion_with_minimax_options(temp_db: None, completion_api: CompletionAPI) -> None:
    from fastapi.testclient import TestClient

    from app.web.main import app

    calls = completion_api([_response("pong")])
    with TestClient(app) as client:
        response = client.post(
            "/api/llm-providers/test",
            json={"providers": [_provider("https://api.minimax.cn/v1", "MiniMax-M3.1-Flash").to_dict()]},
        )
    assert response.status_code == 200
    assert response.json()["results"][0]["ok"] is True
    assert response.json()["results"][0]["detail"] == "pong"
    assert calls[0]["max_completion_tokens"] == 4096
    assert calls[0]["extra_body"] == {"reasoning_effort": "low"}
    assert llm_providers._read_raw() == []
