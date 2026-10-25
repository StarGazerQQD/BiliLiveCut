from __future__ import annotations

import json
from types import SimpleNamespace

import httpx2 as httpx
import pytest

from app.analysis import llm, llm_providers
from app.core.runtime_settings import settings_scope
from app.models.configuration import ModelAuthorizationError, save_model_configuration
from app.models.contracts import ConnectionChange, ModelConfigurationChange, ModelRoute


def catalog(revision: int = 0) -> ModelConfigurationChange:
    return ModelConfigurationChange(
        revision=revision,
        connections=[
            ConnectionChange(id="shared", name="测试连接", base_url="https://model.example/v1", api_key="secret-old")
        ],
        routes=[
            ModelRoute(id="analysis", name="分析", connection_id="shared", capability="analysis", model="original")
        ],
    )


@pytest.mark.parametrize("change", ["disabled", "removed", "cleared", "rotated", "endpoint", "route"])
def test_old_snapshot_cannot_send_after_authorization_change(temp_db: None, change: str) -> None:
    save_model_configuration(catalog())
    calls = []
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs)))
    )
    with settings_scope():
        provider = llm_providers.active_providers()[0]
        updated = catalog(1)
        if change == "disabled":
            updated.connections[0].enabled = False
        elif change == "removed":
            updated.connections.clear()
            updated.routes.clear()
        elif change == "cleared":
            updated.connections[0].api_key = ""
            updated.connections[0].clear_api_key = True
        elif change == "rotated":
            updated.connections[0].api_key = "secret-new"
        elif change == "endpoint":
            updated.connections[0].base_url = "https://other.example/v1"
            updated.connections[0].api_key = "secret-new"
        else:
            updated.routes[0].enabled = False
        save_model_configuration(updated)
        assert provider.api_key == "secret-old"  # 快照不被就地改写。
        with pytest.raises(ModelAuthorizationError) as error:
            llm._create_completion(client, provider, "private content", 32, None)
        assert "secret-" not in str(error.value)
        assert calls == []


def test_model_change_keeps_snapshot_parameters_but_new_task_uses_current_key(temp_db: None) -> None:
    save_model_configuration(catalog())
    calls = []
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs)))
    )
    with settings_scope():
        updated = catalog(1)
        updated.routes[0].model = "updated-model"
        save_model_configuration(updated)
        old = llm_providers.active_providers()[0]
        llm._create_completion(client, old, "test", 32, None)
        assert calls[0]["model"] == "original"
    rotated = catalog(2)
    rotated.connections[0].api_key = "secret-new"
    save_model_configuration(rotated)
    current = llm_providers.active_providers()[0]
    assert current.api_key == "secret-new"
    current.authorize()


def test_empty_response_retry_rechecks_live_authorization(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    save_model_configuration(catalog())
    requests = []

    def transport(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        disabled = catalog(1)
        disabled.connections[0].enabled = False
        save_model_configuration(disabled)
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "original",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
            },
            request=request,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    with settings_scope():
        provider = llm_providers.active_providers()[0]
        with pytest.raises(ModelAuthorizationError):
            llm._complete(provider, "retry test", 32)
    assert len(requests) == 1
    assert json.loads(requests[0].content)["model"] == "original"


def test_legacy_provider_cannot_reuse_revoked_snapshot(temp_db: None) -> None:
    provider = llm_providers.LLMProvider("legacy", "旧配置", "https://model.example/v1", "old-secret", "model")
    llm_providers.save_providers([provider])
    with settings_scope():
        old = llm_providers.active_providers()[0]
        provider.enabled = False
        llm_providers.save_providers([provider])
        with pytest.raises(ModelAuthorizationError):
            old.authorize()


def test_unsaved_probe_allows_first_call_but_blocks_retry_after_configuration_change(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.contracts import ModelProbe
    from app.web.services.settings import test_model_route as probe

    calls = []

    def transport(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        save_model_configuration(catalog())
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "original",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
            },
            request=request,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    result = probe(ModelProbe(**catalog().model_dump(), route_id="analysis"))
    assert not result.ok and "配置已变更" in result.detail
    assert len(calls) == 1
