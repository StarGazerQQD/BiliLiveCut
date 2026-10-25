"""真实 HTTP 与生产 ASR 子进程之间的完成、复用和取消验证。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import psutil
import pytest

from app.analysis.transcription.isolated import IsolatedPipeline
from app.analysis.transcription.process_pool import ASRProcessPool
from app.core.config import settings
from app.core.configuration import ConfigurationChange, save_configuration
from app.models.configuration import save_model_configuration
from app.models.contracts import ConnectionChange, ModelConfigurationChange, ModelRoute
from app.pipeline.lease import LeaseLostError


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("routed", [False, True])
def test_real_remote_process_reuses_slot_or_stops_blocked_request(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool, routed: bool
) -> None:
    received = threading.Event()
    release = threading.Event()
    paths: list[str] = []
    uploads: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            paths.append(self.path)
            uploads.append(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers["Authorization"] == "Bearer process-fixture-key"
            received.set()
            if cancel:
                assert release.wait(30)
            payload = json.dumps({"text": "远程转写已经通过真实子进程返回。"}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                assert cancel

    server = HTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    serving.start()
    monkeypatch.setattr(settings, "asr_remote_base_url", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setattr(settings, "asr_remote_model", "fixture-only-model")
    monkeypatch.setattr(settings, "asr_remote_api_key", "process-fixture-key")
    monkeypatch.setattr(settings, "asr_primary", "openai_compatible")
    monkeypatch.setattr(settings, "asr_fallback_whisper", False)
    monkeypatch.setattr(settings, "asr_remote_timeout_s", 30)
    if routed:
        save_model_configuration(
            ModelConfigurationChange(
                revision=0,
                connections=[
                    ConnectionChange(
                        id="speech",
                        name="本地协议测试",
                        base_url=f"http://127.0.0.1:{server.server_port}/v1",
                        api_key="process-fixture-key",
                    )
                ],
                routes=[
                    ModelRoute(
                        id="speech",
                        name="远程测试",
                        capability="asr",
                        connection_id="speech",
                        model="fixture-only-model",
                    )
                ],
            )
        )
        save_configuration(
            ConfigurationChange(
                values={
                    "asr_remote_route_id": "speech",
                    "asr_remote_base_url": "",
                    "asr_remote_model": "",
                    "asr_remote_api_key": "",
                }
            )
        )
    pool = ASRProcessPool()
    pids: list[int] = []

    def check() -> None:
        if cancel and received.is_set():
            pids.extend(slot.process.pid for slot in pool._slots if slot.process.pid is not None)
            raise LeaseLostError("取消正在等待远程 HTTP 响应的任务")

    try:
        pipeline = IsolatedPipeline(pool=pool, check_cancelled=check)
        if cancel:
            with pytest.raises(LeaseLostError):
                pipeline.transcribe(str(pcm_wav))
            assert pids and all(not psutil.pid_exists(pid) for pid in pids)
            assert not pool._slots and all(value == 0 for value in pool._roles.values())
            assert paths == ["/v1/audio/transcriptions"]
        else:
            result = pipeline.transcribe(str(pcm_wav))
            first_pid = pool._slots[0].process.pid
            again = pipeline.transcribe(str(pcm_wav))
            assert result.text == again.text == "远程转写已经通过真实子进程返回。"
            assert result.backend == "openai_compatible" and result.audio_duration == 8.336
            assert result.model_id == "fixture-only-model"
            assert result.segments[0].words == [] and result.segments[0].raw_confidence is None
            assert len(pool._slots) == 1 and pool._slots[0].process.pid == first_pid
            assert paths == ["/v1/audio/transcriptions"] * 2
        assert uploads and all(b'filename="audio.wav"' in upload and b"RIFF" in upload for upload in uploads)
    finally:
        pool.shutdown(0)
        release.set()
        server.shutdown()
        serving.join(timeout=5)
        server.server_close()
        assert not serving.is_alive()


@pytest.mark.parametrize("routed", [False, True])
def test_revocation_between_chunks_stops_next_upload(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, routed: bool
) -> None:
    from app.core.runtime_settings import settings_scope
    from app.models.configuration import ModelAuthorizationError

    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            calls.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            # 第一块已经提交给服务端；撤销后，父进程不得许可第二块上传。
            if routed:
                revised = definition.model_copy(deep=True)
                revised.revision = 1
                revised.connections[0].api_key = ""
                revised.connections[0].clear_api_key = True
                save_model_configuration(revised)
            else:
                save_configuration(ConfigurationChange(clear=["asr_remote_api_key"]))
            payload = json.dumps({"text": "第一块的有效文本"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    monkeypatch.setattr(settings, "asr_primary", "openai_compatible")
    monkeypatch.setattr(settings, "asr_remote_base_url", endpoint)
    monkeypatch.setattr(settings, "asr_remote_api_key", "revocable-key")
    monkeypatch.setattr(settings, "asr_remote_model", "fixture-model")
    monkeypatch.setattr(settings, "asr_remote_chunk_s", 4)
    monkeypatch.setattr(settings, "asr_fallback_whisper", False)
    definition = ModelConfigurationChange(
        revision=0,
        connections=[ConnectionChange(id="speech", name="测试", base_url=endpoint, api_key="revocable-key")],
        routes=[ModelRoute(id="speech", name="测试", connection_id="speech", capability="asr", model="fixture-model")],
    )
    if routed:
        save_model_configuration(definition)
        monkeypatch.setattr(settings, "asr_remote_route_id", "speech")
    else:
        # 模型目录存在也不应阻止独立 ASR 配置；二者的授权来源必须区分。
        save_model_configuration(ModelConfigurationChange(revision=0, connections=[], routes=[]))
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    serving.start()
    pool = ASRProcessPool()
    try:
        with settings_scope():
            with pytest.raises(ModelAuthorizationError):
                IsolatedPipeline(pool=pool).transcribe(str(pcm_wav))
        assert calls == ["/v1/audio/transcriptions"]
        assert not pool._slots and all(value == 0 for value in pool._roles.values())
    finally:
        pool.shutdown(0)
        server.shutdown()
        serving.join(timeout=5)
        server.server_close()
        assert not serving.is_alive()
