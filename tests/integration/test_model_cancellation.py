from __future__ import annotations

import asyncio
import gzip
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import pytest

from app.analysis import llm
from app.core.cancellation import OperationShutdownError, cancellation_scope, shutdown_event
from app.core.config import settings
from app.models.configuration import save_model_configuration
from app.models.contracts import ConnectionChange, ModelConfigurationChange, ModelRoute


@pytest.fixture(autouse=True)
def direct_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = os.environ.get("no_proxy", os.environ.get("NO_PROXY", ""))
    value = ",".join(part for part in (existing, "127.0.0.1", "localhost") if part)
    monkeypatch.setenv("NO_PROXY", value)
    monkeypatch.setenv("no_proxy", value)


def _configure(url: str) -> None:
    save_model_configuration(
        ModelConfigurationChange(
            revision=0,
            connections=[ConnectionChange(id="cancel-http", name="本地协议测试", base_url=url, api_key="fixture-key")],
            routes=[
                ModelRoute(
                    id=f"{capability}-{index}",
                    name="本地协议测试",
                    connection_id="cancel-http",
                    capability=capability,
                    model=f"fixture-{index}",
                    max_output_tokens=512,
                    priority=index,
                )
                for capability in ("analysis", "trends", "copywriting")
                for index in range(2)
            ],
        )
    )


def _close_fixture_client() -> None:
    with llm._client_cache_lock:
        for key in [key for key in llm._client_cache if key[0] == "cancel-http"]:
            llm._client_cache.pop(key).close()


@pytest.mark.parametrize("interruption", ["job", "task", "lease", "shutdown"])
async def test_manual_render_cancellation_closes_http_and_preserves_task_contract(
    temp_db: None, tmp_path: Path, interruption: str
) -> None:
    from sqlmodel import select

    from app.db.entities import (
        ClipVariant,
        FinalClip,
        HighlightCandidate,
        HighlightEvent,
        LiveRoom,
        RawSegment,
        RecordingSession,
        ReviewStatus,
        SegmentTask,
        TaskStatus,
        Transcript,
    )
    from app.db.job_store import get_job
    from app.db.session import get_session
    from app.pipeline.task_worker import cancel_task
    from app.web.services.background_jobs import WebJobManager
    from tests.unit.test_clipping import _make_test_ts

    source = tmp_path / "source.ts"
    assert _make_test_ts(source, duration=3)
    base = datetime.now(UTC)
    with get_session() as db:
        room = LiveRoom(input_url="fixture", room_id=1, authorized=True)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id, started_at=base)
        db.add(session)
        db.flush()
        segment = RawSegment(
            session_id=session.id,
            seq=0,
            file_path=str(source),
            start_ts=base,
            end_ts=base + timedelta(seconds=3),
            duration_s=3,
        )
        db.add(segment)
        db.flush()
        db.add(Transcript(segment_id=segment.id, final_text="真实本地素材", words_json="[]"))
        candidate = HighlightCandidate(
            session_id=session.id,
            start_ts=base,
            end_ts=base + timedelta(seconds=2),
            peak_ts=base + timedelta(seconds=1),
            dedup_hash="manual-http-cancel",
        )
        db.add(candidate)
        db.flush()
        event = HighlightEvent(
            candidate_id=candidate.id,
            session_id=session.id,
            segment_id=segment.id,
            raw_start_ts=candidate.start_ts,
            raw_end_ts=candidate.end_ts,
            adjusted_start_ts=candidate.start_ts,
            adjusted_end_ts=candidate.end_ts,
            review_status=ReviewStatus.APPROVED_SOLO,
        )
        db.add(event)
        db.flush()
        task = SegmentTask(
            segment_id=segment.id,
            session_id=session.id,
            candidate_id=candidate.id,
            event_id=event.id,
            stage=TaskStatus.APPROVED_WAITING_RENDER,
        )
        db.add(task)
        db.flush()
        task_id, candidate_id = task.id, candidate.id
    received, closed = Event(), Event()
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(self.path)
            received.set()
            self.connection.settimeout(10)
            if self.connection.recv(1) == b"":
                closed.set()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = Thread(target=server.serve_forever)
    server_thread.start()
    _configure(f"http://127.0.0.1:{server.server_port}/v1")
    manager = WebJobManager(max_concurrency=1)
    try:
        job = await manager.enqueue(
            "candidate_render",
            {"candidate_id": candidate_id, "reviewed_by": "fixture"},
            label="本地真实出片取消",
            owner="fixture",
        )
        deadline = time.monotonic() + 30
        while not received.is_set():
            current = get_job(job["id"])
            assert current["status"] not in {"failed", "cancelled", "succeeded"}, current
            assert time.monotonic() < deadline, current
            await asyncio.sleep(0.02)
        with get_session() as db:
            task = db.get(SegmentTask, task_id)
            assert task.stage == TaskStatus.RENDERING
            attempts = task.attempts
        generated = list((tmp_path / "storage").rglob("*.mp4"))
        assert generated and all(path.stat().st_size > 0 for path in generated)
        if interruption == "job":
            manager.cancel(job["id"], "fixture")
        elif interruption == "task":
            assert cancel_task(task_id)
        elif interruption == "lease":
            with get_session() as db:
                task = db.get(SegmentTask, task_id)
                task.claimed_by, task.lease_token = "replacement-worker", "replacement-lease"
                db.add(task)
        else:
            shutdown_event.set()
        deadline = time.monotonic() + 5
        while get_job(job["id"])["status"] not in {"cancelled", "failed", "succeeded"}:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        assert get_job(job["id"])["status"] == "cancelled"
        assert await asyncio.to_thread(closed.wait, 2)
        assert len(requests) == 1
        with get_session() as db:
            task = db.get(SegmentTask, task_id)
            assert task.last_error is None
            if interruption == "shutdown":
                assert task.attempts == attempts
                assert task.stage == TaskStatus.QUEUED_FOR_RENDER and task.lease_token is None
            elif interruption == "lease":
                assert task.attempts == attempts
                assert task.stage == TaskStatus.RENDERING
                assert task.claimed_by == "replacement-worker" and task.lease_token == "replacement-lease"
            else:
                assert task.attempts == 0
                assert task.stage == TaskStatus.CANCELLED and task.lease_token is None
            assert not db.exec(select(FinalClip)).all()
            assert not db.exec(select(ClipVariant)).all()
        assert not list((tmp_path / "storage").rglob("*.mp4"))
        assert not list((tmp_path / "storage").rglob("*.jpg"))
        assert source.is_file()
    finally:
        shutdown_event.set()
        await manager.stop(timeout_s=10)
        _close_fixture_client()
        server.shutdown()
        server.server_close()
        server_thread.join(3)


@pytest.mark.parametrize("partial_body", [False, True])
@pytest.mark.parametrize("search", [False, True])
def test_shutdown_closes_real_http_and_prevents_route_retry(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, partial_body: bool, search: bool
) -> None:
    received, closed = Event(), Event()
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(self.path)
            if partial_body:
                self.send_response(200)
                self.send_header("Content-Length", "10000")
                self.end_headers()
                self.wfile.write(b" ")
                self.wfile.flush()
            received.set()
            self.connection.settimeout(5)
            if self.connection.recv(1) == b"":
                closed.set()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = Thread(target=server.serve_forever)
    server_thread.start()
    _configure(f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setattr(settings, "llm_request_timeout_s", 600)
    operation = llm.call_web_search if search else llm.call_text
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            execution = executor.submit(operation, "取消协议测试", 512)
            try:
                assert received.wait(5)
                shutdown_event.set()
                with pytest.raises(OperationShutdownError):
                    execution.result(timeout=3)
                assert closed.wait(2)
                assert len(requests) == 1
                with pytest.raises(OperationShutdownError):
                    operation("停止后不得发送", 512)
                assert len(requests) == 1
            finally:
                shutdown_event.set()
    finally:
        _close_fixture_client()
        server.shutdown()
        server.server_close()
        server_thread.join(3)


def test_sync_sdk_reuses_real_connection_and_decodes_compressed_response(temp_db: None) -> None:
    peers: list[tuple[str, int]] = []
    response = gzip.compress(
        json.dumps(
            {
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture-0",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "正文"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            peers.append(self.client_address)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            self.wfile.flush()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = Thread(target=server.serve_forever)
    server_thread.start()
    _configure(f"http://127.0.0.1:{server.server_port}/v1")
    try:
        assert llm.call_text("第一个请求", 512) == "正文"
        assert llm.call_text("第二个请求", 512) == "正文"
        assert len(peers) == 2 and peers[0] == peers[1]
    finally:
        _close_fixture_client()
        server.shutdown()
        server.server_close()
        server_thread.join(3)


def test_cancelling_one_request_does_not_close_other_request_on_same_client(temp_db: None) -> None:
    from app.core.cancellation import OperationCancelledError

    first_received, second_received, first_closed = Event(), Event(), Event()
    cancel_first, release_second = Event(), Event()
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = request["messages"][-1]["content"]
            received.append(prompt)
            if prompt == "cancel-first":
                first_received.set()
                self.connection.settimeout(5)
                if self.connection.recv(1) == b"":
                    first_closed.set()
                return
            second_received.set()
            assert release_second.wait(5)
            response = json.dumps(
                {
                    "id": "independent",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "fixture-0",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "still active"},
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            self.wfile.flush()

        def log_message(self, format: str, *args: object) -> None:
            pass

    def first() -> str:
        with cancellation_scope(cancel_first.is_set):
            return llm.call_text("cancel-first", 512)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = Thread(target=server.serve_forever)
    server_thread.start()
    _configure(f"http://127.0.0.1:{server.server_port}/v1")
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            cancelled = executor.submit(first)
            try:
                assert first_received.wait(3)
                active = executor.submit(llm.call_text, "second", 512)
                assert second_received.wait(3)
                cancel_first.set()
                with pytest.raises(OperationCancelledError):
                    cancelled.result(timeout=3)
                assert first_closed.wait(2)
                assert not active.done()
                release_second.set()
                assert active.result(timeout=3) == "still active"
                assert received == ["cancel-first", "second"]
            finally:
                cancel_first.set()
                release_second.set()
    finally:
        _close_fixture_client()
        server.shutdown()
        server.server_close()
        server_thread.join(3)


@pytest.mark.parametrize("structured", [False, True])
def test_shutdown_closes_network_before_blocked_identity_check_returns(temp_db: None, structured: bool) -> None:
    received, closed, checking, release_check = Event(), Event(), Event(), Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            received.set()
            self.connection.settimeout(5)
            if self.connection.recv(1) == b"":
                closed.set()

        def log_message(self, format: str, *args: object) -> None:
            pass

    def identity_check() -> bool:
        if received.is_set():
            checking.set()
            assert release_check.wait(5)
        return False

    def execute() -> object:
        if structured:
            return llm.call_structured(
                "本地关闭测试",
                {},
                schema={"type": "object", "properties": {}},
                validate=lambda value: value,
                budget=llm.new_editorial_budget(cancelled=identity_check),
                purpose="explanation",
                max_tokens=512,
            )
        with cancellation_scope(identity_check):
            return llm.call_text("本地关闭测试", 512)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = Thread(target=server.serve_forever)
    server_thread.start()
    _configure(f"http://127.0.0.1:{server.server_port}/v1")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(execute)
            try:
                assert received.wait(3) and checking.wait(3)
                shutdown_event.set()
                assert closed.wait(2)
                assert not pending.done()
                release_check.set()
                if structured:
                    assert pending.result(timeout=3).error == "cancelled"
                else:
                    with pytest.raises(OperationShutdownError):
                        pending.result(timeout=3)
            finally:
                shutdown_event.set()
                release_check.set()
    finally:
        _close_fixture_client()
        server.shutdown()
        server.server_close()
        server_thread.join(3)
