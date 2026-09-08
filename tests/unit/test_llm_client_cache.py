"""LLM 客户端复用、完整凭据轮换与替换失败的回归测试。"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from app.analysis import llm
from app.analysis.llm_providers import LLMProvider

if TYPE_CHECKING:
    from pytest import MonkeyPatch


class FakeClient:
    def __init__(self, *, api_key: str, base_url: str | None) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


@pytest.fixture
def clients(monkeypatch: MonkeyPatch) -> list[FakeClient]:
    created: list[FakeClient] = []

    def create(*, api_key: str, base_url: str | None) -> FakeClient:
        client = FakeClient(api_key=api_key, base_url=base_url)
        created.append(client)
        return client

    module = ModuleType("openai")
    module.OpenAI = create
    monkeypatch.setitem(sys.modules, "openai", module)
    monkeypatch.setattr(llm, "_client_cache", {})
    monkeypatch.setattr(llm, "_client_active", {})
    monkeypatch.setattr(llm, "_retired_clients", {})
    return created


def test_reuses_connection_without_caching_credential_or_digest(clients: list[FakeClient]) -> None:
    provider = LLMProvider("cache", "test", "https://example.invalid", "samehead-old", "test")
    first = llm._get_client(provider)

    assert llm._get_client(replace(provider, name="renamed", model="another-model")) is first
    assert len(clients) == 1
    assert clients[0].close_count == 0
    assert list(llm._client_cache) == [(provider.id, provider.base_url)]


@pytest.mark.parametrize(
    ("field", "value"),
    [("api_key", "samehead-new"), ("api_key", "测试完整凭据"), ("base_url", "https://other.invalid")],
)
def test_replaces_changed_credentials_or_endpoint_and_closes_idle_connection(
    clients: list[FakeClient], field: str, value: str
) -> None:
    provider = LLMProvider("rotation", "test", "https://example.invalid", "samehead-old", "test")
    first = llm._get_client(provider)
    changed = replace(provider, **{field: value})

    replacement = llm._get_client(changed)

    assert replacement is not first
    assert llm._get_client(changed) is replacement
    assert len(clients) == 2
    assert clients[0].close_count == 1
    assert clients[1].api_key == changed.api_key
    assert clients[1].base_url == changed.base_url
    assert clients[1].close_count == 0
    assert list(llm._client_cache) == [(changed.id, changed.base_url)]


def test_providers_with_identical_credentials_keep_independent_connections(clients: list[FakeClient]) -> None:
    provider = LLMProvider("first", "test", "https://example.invalid", "samehead-old", "test")
    first = llm._get_client(provider)
    second = llm._get_client(replace(provider, id="second"))

    assert first is not second
    assert llm._get_client(provider) is first
    assert len(clients) == 2
    assert all(client.close_count == 0 for client in clients)


def test_failed_replacement_keeps_existing_connection(clients: list[FakeClient], monkeypatch: MonkeyPatch) -> None:
    provider = LLMProvider("failed", "test", "https://example.invalid", "samehead-old", "test")
    first = llm._get_client(provider)

    def fail(*, api_key: str, base_url: str | None) -> FakeClient:
        raise OSError("client initialization failed")

    monkeypatch.setattr(sys.modules["openai"], "OpenAI", fail)
    with pytest.raises(OSError, match="client initialization failed"):
        llm._get_client(replace(provider, api_key="samehead-new"))

    assert llm._get_client(provider) is first
    assert len(clients) == 1
    assert clients[0].close_count == 0


def test_rotation_waits_for_last_active_request_even_when_it_fails(
    clients: list[FakeClient], monkeypatch: MonkeyPatch
) -> None:
    provider = LLMProvider("active", "test", "https://example.invalid", "samehead-old", "test")
    entered = {name: Event() for name in ("success", "failure")}
    release = {name: Event() for name in entered}

    def completion(client: FakeClient, _provider: LLMProvider, prompt: str, *_args: object) -> dict[str, object]:
        entered[prompt].set()
        assert release[prompt].wait(5)
        assert client.close_count == 0
        if prompt == "failure":
            raise OSError("request failed")
        return {"choices": [{"message": {"content": "success"}}]}

    monkeypatch.setattr(llm, "_create_completion", completion)
    with ThreadPoolExecutor(max_workers=2) as executor:
        success = executor.submit(llm._complete, provider, "success", 10)
        failure = executor.submit(llm._complete, provider, "failure", 10)
        try:
            assert all(event.wait(5) for event in entered.values())
            replacement = llm._get_client(replace(provider, api_key="samehead-new"))
            assert len(clients) == 2
            assert clients[0].close_count == 0
            release["success"].set()
            assert success.result(timeout=5) == "success"
            assert clients[0].close_count == 0
            release["failure"].set()
            with pytest.raises(OSError, match="request failed"):
                failure.result(timeout=5)
        finally:
            for event in release.values():
                event.set()

    assert clients[0].close_count == 1
    assert clients[1].close_count == 0
    assert llm._get_client(replace(provider, api_key="samehead-new")) is replacement
    assert not llm._client_active
    assert not llm._retired_clients
