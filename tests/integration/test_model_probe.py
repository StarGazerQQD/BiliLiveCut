from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlmodel import select

from app.analysis import llm
from app.core.cancellation import OperationShutdownError, shutdown_event
from app.db.entities import AppSetting
from app.db.session import get_session
from app.models.configuration import model_configuration_view, save_model_configuration
from app.models.contracts import ModelProbe
from app.web.services.settings import test_model_route as probe_route
from tests.integration.test_structured_llm import catalog, response

if TYPE_CHECKING:
    from pytest import MonkeyPatch

ProbeTransport = Callable[[str], list[httpx.Request]]


@pytest.fixture
def probe_transport(monkeypatch: MonkeyPatch) -> ProbeTransport:
    def install(mode: str = "success") -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
            requests.append(request)
            sent = json.loads(request.content)
            if mode == f"shutdown-{len(requests)}":
                shutdown_event.set()
            if mode == "rotate":
                rotated = catalog(1)
                rotated.connections[0].api_key = "rotated-secret"
                save_model_configuration(rotated)
            if mode == "denied":
                return httpx.Response(401, json={"error": {"message": "private-test-key"}})
            if "response_format" in sent:
                if mode == "unsupported":
                    return httpx.Response(
                        400,
                        json={
                            "error": {
                                "param": "response_format",
                                "code": "unsupported_parameter",
                                "message": "response_format is not supported",
                            }
                        },
                    )
                answer = '{"answer":"pong"}'
            else:
                answer = "" if mode == "empty" else "pong"
            return httpx.Response(200, json=response(answer), headers={"x-request-id": f"probe-{len(requests)}"})

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
        return requests

    return install


def _stored_diagnostics() -> list[str]:
    with get_session() as db:
        return list(db.exec(select(AppSetting.value).where(AppSetting.key.startswith("model_probe:v1:"))).all())


def test_saved_route_probe_records_actual_observations_without_configuration_revision_change(
    temp_db: None, probe_transport: ProbeTransport
) -> None:
    save_model_configuration(catalog())
    requests = probe_transport("success")
    try:
        result = probe_route(ModelProbe(**catalog(1).model_dump(), route_id="route0"))
        assert result.ok and result.persisted
        diagnostic = result.diagnostic
        assert diagnostic is not None and diagnostic.text == diagnostic.structured == "accepted"
        assert diagnostic.reasoning_parameter == "not_sent"
        assert diagnostic.attempts == 2 and diagnostic.output_limits == [512, 512]
        assert diagnostic.request_ids == ["probe-1", "probe-2"]
        assert diagnostic.estimated_cost_usd == pytest.approx(0.0001)
        assert len(requests) == 2 and all(json.loads(item.content)["model"] == "model0" for item in requests)
        view = model_configuration_view()
        assert view.revision == 1 and view.probe_diagnostics == [diagnostic]
        raw = _stored_diagnostics()
        assert len(raw) == 1
        assert "private-test-key" not in raw[0]
        assert hashlib.sha256(b"private-test-key").hexdigest() not in raw[0]
        assert "messages" not in raw[0] and "choices" not in raw[0]
        assert len(requests) == 2
    finally:
        llm.close_clients()


@pytest.mark.parametrize("draft", ["unsaved", "model", "endpoint", "credential"])
def test_draft_probe_never_persists_diagnostic(temp_db: None, probe_transport: ProbeTransport, draft: str) -> None:
    change = catalog()
    if draft != "unsaved":
        save_model_configuration(catalog())
        change.revision = 1
    if draft == "model":
        change.routes[0].model = "draft-model"
    elif draft == "endpoint":
        change.connections[0].base_url = "https://draft.invalid/v1"
    elif draft == "credential":
        change.connections[0].api_key = "draft-secret"
    requests = probe_transport("success")
    try:
        result = probe_route(ModelProbe(**change.model_dump(), route_id="route0"))
        assert result.ok and not result.persisted
        assert len(requests) == 2 and _stored_diagnostics() == []
        assert model_configuration_view().revision == (0 if draft == "unsaved" else 1)
    finally:
        llm.close_clients()


@pytest.mark.parametrize(
    "mode,attempts,structured", [("unsupported", 2, "rejected"), ("empty", 2, "unknown"), ("denied", 1, "unknown")]
)
def test_probe_failure_and_unrun_protocol_remain_distinct(
    temp_db: None, probe_transport: ProbeTransport, mode: str, attempts: int, structured: str
) -> None:
    save_model_configuration(catalog())
    requests = probe_transport(mode)
    try:
        result = probe_route(ModelProbe(**catalog(1).model_dump(), route_id="route0"))
        assert result.persisted and result.diagnostic is not None
        assert result.diagnostic.structured == structured
        assert result.ok is (mode == "unsupported")
        assert result.diagnostic.attempts == len(requests) == attempts
        if mode == "denied":
            assert result.diagnostic.estimated_cost_usd is None
        assert "private-test-key" not in _stored_diagnostics()[0]
    finally:
        llm.close_clients()


def test_rotation_during_probe_blocks_followup_and_late_diagnostic(
    temp_db: None, probe_transport: ProbeTransport
) -> None:
    save_model_configuration(catalog())
    requests = probe_transport("rotate")
    try:
        result = probe_route(ModelProbe(**catalog(1).model_dump(), route_id="route0"))
        assert not result.ok and not result.persisted
        assert "配置已变更" in result.detail
        assert len(requests) == 1 and _stored_diagnostics() == []
        assert model_configuration_view().revision == 2
    finally:
        llm.close_clients()


@pytest.mark.parametrize("attempt", [1, 2])
def test_shutdown_during_probe_preserves_usage_without_persisting_diagnostic(
    temp_db: None, probe_transport: ProbeTransport, attempt: int
) -> None:
    save_model_configuration(catalog())
    requests = probe_transport(f"shutdown-{attempt}")
    try:
        with pytest.raises(OperationShutdownError):
            probe_route(ModelProbe(**catalog(1).model_dump(), route_id="route0"))
        assert len(requests) == attempt and _stored_diagnostics() == []
        assert llm._today_spend() == pytest.approx(0.00005 * attempt)
    finally:
        shutdown_event.clear()
        llm.close_clients()


@pytest.mark.parametrize("changed", ["name", "model", "credential", "endpoint", "removed"])
def test_saved_diagnostics_invalidate_only_affected_requests(
    temp_db: None, probe_transport: ProbeTransport, changed: str
) -> None:
    configuration = catalog(routes=2)
    configuration.connections.append(configuration.connections[0].model_copy(update={"id": "independent"}))
    configuration.routes[1].connection_id = "independent"
    save_model_configuration(configuration)
    requests = probe_transport("success")
    configuration.revision = 1
    try:
        for route_id in ("route0", "route1"):
            assert probe_route(ModelProbe(**configuration.model_dump(), route_id=route_id)).persisted
        assert len(requests) == 4
        if changed == "name":
            configuration.routes[0].name = "仅改名称"
        elif changed == "model":
            configuration.routes[0].model = "new-model"
        elif changed == "credential":
            configuration.connections[0].api_key = "new-secret"
        elif changed == "endpoint":
            configuration.connections[0].base_url = "https://new.invalid/v1"
        else:
            configuration.routes.pop(0)
        result = save_model_configuration(configuration)
        assert [item.route_id for item in result.probe_diagnostics] == (
            ["route0", "route1"] if changed == "name" else ["route1"]
        )
        assert result.revision == 2 and len(requests) == 4
    finally:
        llm.close_clients()
