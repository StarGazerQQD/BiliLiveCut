from __future__ import annotations

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.analysis import llm
from app.analysis.model_execution import ModelCallBudget
from app.analysis.model_journal import ModelJournalError, ModelRequestJournal, ModelRequestRecord
from app.db.entities import AppSetting, SystemLog
from app.db.session import get_session
from app.models.configuration import save_model_configuration
from tests.integration.test_structured_llm import call, catalog, response


def _records() -> list[ModelRequestRecord]:
    with get_session() as db:
        rows = db.exec(select(AppSetting).where(AppSetting.key.startswith("model_request:v1:"))).all()
        return [ModelRequestRecord.model_validate_json(row.value) for row in rows]


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("echo_secret", [False, True])
def test_real_sdk_persists_identity_and_receipt_without_payload_or_credentials(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, structured: bool, echo_secret: bool
) -> None:
    save_model_configuration(catalog())
    seen: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        pending = _records()
        assert len(pending) == 1 and pending[0].state == "unconfirmed"
        assert pending[0].usage.estimated_cost_usd is None
        return httpx.Response(
            200,
            json=response(model="private-test-key" if echo_secret else "reported-model"),
            headers={"x-request-id": "private-test-key" if echo_secret else "actual-receipt"},
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = llm.new_editorial_budget(target="hotspot:23")
    try:
        if structured:
            assert call(budget).value is not None
        else:
            assert llm.call_text("private-transcript-text", budget=budget)
        (record,) = _records()
        assert record.operation_id == budget.operation_id and record.target == "hotspot:23"
        assert record.state == "finished" and record.attempt.status == "success"
        assert record.request_id == (None if echo_secret else "actual-receipt") and record.received_at is not None
        assert record.attempt.reported_model == (None if echo_secret else "reported-model")
        assert record.output_tokens_limit == 512 and record.usage.estimated_cost_usd == pytest.approx(0.00005)
        assert record.attempt == budget.attempts[0]
        assert len(record.input_sha256) == 64 and len(seen) == 1
        with get_session() as db:
            logs = db.exec(select(SystemLog).where(SystemLog.module == "llm")).all()
        serialized = record.model_dump_json() + json.dumps([item.model_dump(mode="json") for item in logs])
        for secret in ("private-test-key", "private-transcript-text", "忽略系统", "有效正文", "fixture.invalid"):
            assert secret not in serialized
        assert {item.event for item in logs} == {"model_request_started", "model_request_finished"}
    finally:
        llm.close_clients()


@pytest.mark.parametrize("failure_at", ["unconfirmed", "response_received"])
@pytest.mark.parametrize("structured", [False, True])
def test_journal_storage_failure_stops_remaining_routes_and_keeps_actual_usage(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, structured: bool, failure_at: str
) -> None:
    save_model_configuration(catalog(routes=2))
    original_commit = Session.commit
    seen: list[httpx.Request] = []

    def commit(db: Session) -> None:
        if any(
            isinstance(row, AppSetting)
            and row.key.startswith("model_request:v1:")
            and json.loads(row.value)["state"] == failure_at
            for row in [*db.new, *db.dirty]
        ):
            raise OperationalError("controlled journal write failure", {}, OSError("storage unavailable"))
        original_commit(db)

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=response(), headers={"x-request-id": "actual-receipt"})

    monkeypatch.setattr(Session, "commit", commit)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = llm.new_editorial_budget()
    try:
        for _ in range(2):
            with pytest.raises(ModelJournalError):
                if structured:
                    call(budget)
                else:
                    llm.call_text("正文", budget=budget)
        assert len(seen) == (0 if failure_at == "unconfirmed" else 1)
        if seen:
            assert budget.attempts[0].usage.estimated_cost_usd == pytest.approx(0.00005)
            (record,) = _records()
            assert record.state == "unconfirmed" and record.usage.estimated_cost_usd is None
        else:
            assert _records() == [] and budget.attempts == ()
    finally:
        llm.close_clients()


def test_budget_ledger_failure_retains_receipt_and_does_not_repeat_charge(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_model_configuration(catalog(routes=2))
    replace = os.replace
    budget_path = llm._budget_file()
    seen: list[httpx.Request] = []

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == budget_path:
            raise OSError("controlled budget storage failure")
        replace(source, destination)

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=response(), headers={"x-request-id": "charged-response"})

    monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = llm.new_editorial_budget()
    with pytest.raises(llm.BudgetAccountingError):
        call(budget)
    (record,) = _records()
    assert record.received_at is not None and record.request_id == "charged-response"
    assert record.usage.estimated_cost_usd == pytest.approx(0.00005)
    assert record.attempt.status == "failed" and len(seen) == 1
    assert llm._today_spend() == 0


def _journal_child() -> None:
    from app.db.session import init_db

    init_db()
    llm.call_text("只发给本地测试服务器", budget=llm.new_editorial_budget(target="summary:1"))


def test_process_interruption_keeps_unknown_cost_and_refuses_same_request_identity(
    temp_db: None, tmp_path: Path
) -> None:
    arrived = Event()
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            arrived.set()
            release.wait(20)
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    save_model_configuration(catalog(base_url=f"http://127.0.0.1:{server.server_port}/v1"))
    environment = dict(os.environ, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    error_path = tmp_path / "model-child.log"
    error_log = error_path.open("wb")
    child = subprocess.Popen(
        [sys.executable, "-c", "from tests.integration.test_model_journal import _journal_child; _journal_child()"],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=error_log,
    )
    try:
        assert arrived.wait(15), error_path.read_text(encoding="utf-8", errors="replace")
        child.kill()
        child.wait(timeout=5)
        (record,) = _records()
        assert record.state == "unconfirmed" and record.attempt is None and record.received_at is None
        assert record.usage.estimated_cost_usd is None and record.request_id is None
        with pytest.raises(ModelJournalError, match="无法保存"):
            ModelRequestJournal().start(
                operation_id=record.operation_id,
                number=record.number,
                provider_id=record.provider_id,
                connection_id=record.connection_id,
                model=record.model,
                purpose=record.purpose,
                protocol=record.protocol,
                output_tokens_limit=record.output_tokens_limit,
                request={},
            )
        assert _records() == [record]
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
        error_log.close()
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_plain_budget_has_no_persistent_journal(temp_db: None) -> None:
    budget = ModelCallBudget(max_attempts=1, max_elapsed_s=1)
    budget.start_request(
        provider_id="route",
        connection_id="connection",
        model="model",
        purpose="text",
        protocol="text",
        output_tokens_limit=128,
        request={"text": "input"},
    )
    assert _records() == []
