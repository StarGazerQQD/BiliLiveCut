from __future__ import annotations

import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.analysis import llm, llm_providers
from app.core.config import Settings, settings
from app.core.configuration import ConfigurationChange, ConfigurationConflict, configuration_view, save_configuration
from app.core.runtime_settings import settings_scope
from app.core.settings_store import get_setting
from app.models.configuration import (
    asr_worker_settings,
    configured_models,
    model_configuration_view,
    save_model_configuration,
)
from app.models.contracts import TEXT_CAPABILITIES, ConnectionChange, ModelConfigurationChange, ModelRoute

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch


def change(revision: int = 0, key: str = "private-key") -> ModelConfigurationChange:
    return ModelConfigurationChange(
        revision=revision,
        connections=[ConnectionChange(id="shared", name="共享服务", base_url="https://model.example/v1", api_key=key)],
        routes=[
            ModelRoute(
                id=capability,
                name=capability,
                connection_id="shared",
                capability=capability,
                model=f"model-{capability}",
                max_output_tokens=1024,
            )
            for capability in TEXT_CAPABILITIES
        ]
        + [ModelRoute(id="remote", name="远程转写", connection_id="shared", capability="asr", model="remote-asr")],
    )


def test_catalog_is_atomic_redacted_and_shares_configuration_revision(temp_db: None) -> None:
    result = save_model_configuration(change())
    assert result.source == "routes" and result.revision == 1
    assert "private-key" not in result.model_dump_json()
    assert result.connections[0].api_key_set
    assert all(count == 1 for count in result.active_counts.values())
    assert configuration_view()["revision"] == 1
    save_configuration(ConfigurationChange(values={"clip_video_crf": 29}, revision=1))
    with pytest.raises(ConfigurationConflict):
        save_model_configuration(change(1, "stale-key"))
    assert configured_models().connections[0].api_key == "private-key"
    assert model_configuration_view().revision == 2


@pytest.mark.parametrize("value", [" ", "\u3000", " model", "model ", "model\n"])
def test_route_rejects_blank_or_ambiguous_model_identity(value: str) -> None:
    with pytest.raises(ValidationError):
        ModelRoute(id="route", name="名称", connection_id="connection", capability="analysis", model=value)


def test_text_entrypoints_reject_asr_before_accessing_network(temp_db: None) -> None:
    save_model_configuration(change())
    for operation in (llm.call_text, llm.call_web_search):
        with pytest.raises(ValueError, match="ASR 必须使用语音转写入口"):
            operation("禁止发送到文字协议", capability="asr")


def test_cli_lists_selected_capability_without_credentials(temp_db: None) -> None:
    from app.cli import app

    save_model_configuration(change())
    result = CliRunner().invoke(app, ["llm-list", "--capability", "summary"], terminal_width=160)
    assert result.exit_code == 0, result.output
    assert "model-summary" in result.output and "model-analysis" not in result.output
    assert "private-key" not in result.output and "****-key" not in result.output
    assert CliRunner().invoke(app, ["llm-test", "--capability", "asr"]).exit_code == 2


def test_parallel_capabilities_keep_every_usage_record(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(llm, "_budget_file", lambda: tmp_path / "budget.json")
    barrier = Barrier(4)

    def account() -> None:
        barrier.wait(timeout=5)
        for _ in range(30):
            llm._add_spend(0.01)

    with ThreadPoolExecutor(max_workers=4) as executor:
        calls = [executor.submit(account) for _ in range(4)]
        for call in calls:
            call.result(timeout=20)
    assert llm._today_spend() == pytest.approx(1.2)


def test_corrupted_budget_cannot_be_interpreted_as_zero(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    path.write_text('{"date":', encoding="utf-8")
    monkeypatch.setattr(llm, "_budget_file", lambda: path)
    with pytest.raises(ValueError, match="预算记录"):
        llm._today_spend()
    with pytest.raises(ValueError, match="预算记录"):
        llm._add_spend(0.01)
    assert path.read_text(encoding="utf-8") == '{"date":'


def _account_in_process(root: str) -> None:
    from app.core.config import get_settings

    os.environ["STORAGE_ROOT"] = root
    get_settings.cache_clear()
    for _ in range(10):
        llm._add_spend(0.01)


def test_separate_processes_share_the_same_budget(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(llm, "_budget_file", lambda: tmp_path / "llm_budget.json")
    with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("spawn")) as executor:
        calls = [executor.submit(_account_in_process, str(tmp_path)) for _ in range(3)]
        for call in calls:
            call.result(timeout=30)
    assert llm._today_spend() == pytest.approx(0.3)


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_budget_transient_replace_adds_the_amount_once(monkeypatch: MonkeyPatch, tmp_path: Path, winerror: int) -> None:
    path = tmp_path / "llm_budget.json"
    monkeypatch.setattr(llm, "_budget_file", lambda: path)
    llm._add_spend(0.1)
    before = path.read_bytes()
    original = os.replace
    calls = 0

    def temporarily_locked(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            assert path.read_bytes() == before
            error = PermissionError("受控 Windows 文件占用")
            error.winerror = winerror
            raise error
        original(source, target)

    monkeypatch.setattr(os, "replace", temporarily_locked)
    llm._add_spend(0.2)
    assert calls == 3
    assert llm._today_spend() == pytest.approx(0.3)
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("winerror, attempts", [(None, 1), (5, 7)])
def test_budget_failed_replace_preserves_the_committed_record(
    monkeypatch: MonkeyPatch, tmp_path: Path, winerror: int | None, attempts: int
) -> None:
    path = tmp_path / "llm_budget.json"
    monkeypatch.setattr(llm, "_budget_file", lambda: path)
    llm._add_spend(0.1)
    before = path.read_bytes()
    calls = 0

    def denied(_source: Path, _target: Path) -> None:
        nonlocal calls
        calls += 1
        error = PermissionError("受控永久文件占用")
        if winerror is not None:
            error.winerror = winerror
        raise error

    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(llm.BudgetAccountingError) as caught:
        llm._add_spend(0.2)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert calls == attempts
    assert path.read_bytes() == before and llm._today_spend() == pytest.approx(0.1)
    assert not list(tmp_path.glob("*.tmp"))


def test_competing_catalog_writes_cannot_overwrite_rotated_credentials(temp_db: None) -> None:
    save_model_configuration(change())
    barrier = Barrier(2)

    def write(key: str) -> str:
        barrier.wait(timeout=5)
        try:
            save_model_configuration(change(1, key))
        except ConfigurationConflict:
            return "conflict"
        return key

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write, "rotated-one")
        second = executor.submit(write, "rotated-two")
        results = [first.result(timeout=10), second.result(timeout=10)]
    assert results.count("conflict") == 1
    assert configured_models().connections[0].api_key == next(item for item in results if item != "conflict")
    assert model_configuration_view().revision == 2


@pytest.mark.parametrize("invalid", ["missing-reference", "duplicate-connection", "duplicate-route", "selected-asr"])
def test_invalid_catalog_preserves_entire_previous_version(temp_db: None, invalid: str) -> None:
    save_model_configuration(change())
    save_configuration(
        ConfigurationChange(values={"asr_remote_route_id": "remote", "asr_primary": "openai_compatible"})
    )
    before = model_configuration_view()
    candidate = change(before.revision)
    if invalid == "missing-reference":
        candidate.routes[0].connection_id = "absent"
    elif invalid == "duplicate-connection":
        candidate.connections.append(candidate.connections[0])
    elif invalid == "duplicate-route":
        candidate.routes.append(candidate.routes[0])
    else:
        candidate.routes[-1].enabled = False
    with pytest.raises(ValueError):
        save_model_configuration(candidate)
    assert model_configuration_view() == before


def test_endpoint_change_requires_explicit_credentials_and_clear_remains_available(temp_db: None) -> None:
    save_model_configuration(change())
    preserved = change(1, "")
    save_model_configuration(preserved)
    assert configured_models().connections[0].api_key == "private-key"
    changed = change(2, "")
    changed.connections[0].base_url = "https://another.example/v1"
    with pytest.raises(ValueError, match="修改服务地址"):
        save_model_configuration(changed)
    assert model_configuration_view().revision == 2
    changed.connections[0].clear_api_key = True
    save_model_configuration(changed)
    assert not configured_models().connections[0].api_key
    assert len(llm_providers.active_providers("summary")) == 1


def test_task_keeps_old_routes_and_asr_worker_receives_only_selected_secret(temp_db: None) -> None:
    initial = change()
    initial.connections.append(
        ConnectionChange(id="asr", name="ASR", base_url="https://asr.example/v1", api_key="asr-key")
    )
    initial.routes[-1].connection_id = "asr"
    save_model_configuration(initial)
    save_configuration(
        ConfigurationChange(values={"asr_primary": "openai_compatible", "asr_remote_route_id": "remote"})
    )
    with settings_scope():
        replacement = initial.model_copy(deep=True)
        replacement.revision = 2
        replacement.connections[1].api_key = "new-asr-key"
        replacement.routes[3].model = "new-summary"
        save_model_configuration(replacement)
        assert llm_providers.active_providers("summary")[0].model == "model-summary"
        payload = asr_worker_settings()
        assert payload["asr_remote_api_key"] == "asr-key"
        assert payload["asr_remote_model"] == "remote-asr"
        assert payload["asr_remote_route_id"] == ""
        assert "private-key" not in json.dumps(payload)
        assert "new-asr-key" not in json.dumps(payload)
        assert Settings.model_validate(payload).asr_primary == "openai_compatible"
    assert llm_providers.active_providers("summary")[0].model == "new-summary"
    assert asr_worker_settings()["asr_remote_api_key"] == "new-asr-key"


def test_legacy_conversion_preserves_all_businesses_and_running_task(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trend_api_key", "trend-key")
    monkeypatch.setattr(settings, "trend_base_url", "https://trends.example/v1")
    monkeypatch.setattr(settings, "trend_model", "trends-model")
    legacy = llm_providers.LLMProvider("legacy", "原服务", "https://model.example/v1", "legacy-key", "legacy-model")
    llm_providers.save_providers([legacy])
    view = model_configuration_view()
    assert view.source == "legacy" and len(view.routes) == 6
    candidate = ModelConfigurationChange(
        revision=view.revision,
        connections=[ConnectionChange(**item.model_dump(exclude={"api_key_set"})) for item in view.connections],
        routes=view.routes,
    )
    with settings_scope():
        save_model_configuration(candidate)
        assert llm_providers.active_providers()[0].id == "legacy"
    assert not get_setting("llm_providers")
    assert llm_providers.active_providers("summary")[0].api_key == "legacy-key"
    assert llm_providers.active_providers("trends")[0].model == "trends-model"
    with pytest.raises(ValueError, match="独立业务路由"):
        llm_providers.save_providers([legacy])


def test_actual_sdk_routes_share_client_and_apply_model_limit_and_accounting(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    async def transport(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": json.loads(request.content)["model"],
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "有效正文"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    config = change()
    for route in config.routes[:-1]:
        route.price_input_per_m = 1
    save_model_configuration(config)
    clients: list[object] = []
    for capability in TEXT_CAPABILITIES:
        assert llm.call_text("真实业务调用", max_tokens=4096, capability=capability) == "有效正文"
        clients.append(llm._get_client(llm_providers.active_providers(capability)[0]))
    assert len({id(client) for client in clients}) == 1
    bodies = [json.loads(request.content) for request in requests]
    assert [body["model"] for body in bodies] == [f"model-{capability}" for capability in TEXT_CAPABILITIES]
    assert all(body["max_tokens"] == 1024 for body in bodies)
    assert all(request.headers["authorization"] == "Bearer private-key" for request in requests)
    assert llm._today_spend() == pytest.approx(0.0005)
    save_configuration(ConfigurationChange(values={"llm_daily_budget": 0.0001}))
    assert llm.call_trend_search("达到预算") is None
    assert len(requests) == 5


def test_accounting_write_failure_does_not_trigger_another_paid_provider(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    calls: list[str] = []

    async def transport(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(
            200,
            json={
                "id": "accounting-failure",
                "object": "chat.completion",
                "created": 1,
                "model": "model-analysis",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "正文"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
            request=request,
        )

    def denied(_source: object, _target: object) -> None:
        raise PermissionError("受控文件写入故障")

    candidate = change()
    candidate.routes[0].price_input_per_m = 1
    candidate.routes.append(candidate.routes[0].model_copy(update={"id": "second", "priority": 200}))
    save_model_configuration(candidate)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(llm.BudgetAccountingError, match="预算记录写入失败"):
        llm.call_text("记账失败后停止")
    assert calls == ["model.example"]


def test_model_api_redacts_invalid_payload_and_enforces_selected_route(temp_db: None) -> None:
    from app.web.main import app

    client = TestClient(app)
    response = client.put("/api/model-configuration", json=change().model_dump())
    assert response.status_code == 200, response.text
    assert "private-key" not in response.text
    assert client.put("/api/model-configuration", json=change().model_dump()).status_code == 409
    bad = change(1).model_dump()
    bad["connections"][0]["base_url"] = "https://user:private-password@model.example"
    invalid = client.put("/api/model-configuration", json=bad)
    assert invalid.status_code == 422
    assert "private-password" not in invalid.text and "private-key" not in invalid.text
    selected = client.patch(
        "/api/settings/configuration",
        json={"values": {"asr_primary": "openai_compatible", "asr_remote_route_id": "remote"}},
    )
    assert selected.status_code == 200, selected.text
    cleared = client.put("/api/model-configuration", json={"revision": 2, "connections": [], "routes": []})
    assert cleared.status_code == 400
    assert model_configuration_view().revision == 2
    probe = client.post("/api/model-configuration/test", json={**change(2, "").model_dump(), "route_id": "remote"})
    assert probe.status_code == 400 and "不会自动上传音频" in probe.text
