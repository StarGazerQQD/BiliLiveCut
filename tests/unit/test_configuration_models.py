"""模型配置变化、活动引用、资源释放和实际加载边界。"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from app.analysis.model_pool import ModelPool
from app.core.config import settings
from app.core.configuration import ConfigurationChange, save_configuration
from app.core.runtime_settings import settings_scope

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_active_model_survives_forced_cleanup_and_exception(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asr_primary_max_concurrency", 1)
    pool = ModelPool()
    model = object()
    with pytest.raises(RuntimeError, match="inference failed"):
        with pool.use("primary", "model", lambda: model) as active:
            assert active is model
            assert pool.cleanup_idle(force=True) == 0
            assert pool.infos()[0]["active_users"] == 1
            raise RuntimeError("inference failed")
    assert pool.infos()[0]["active_users"] == 0
    assert pool.cleanup_idle(force=True) == 1


def test_role_concurrency_waits_until_first_inference_finishes(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asr_primary_max_concurrency", 1)
    pool = ModelPool()
    entered = Event()
    attempted = Event()
    second_entered = Event()
    release = Event()
    model = object()

    def first() -> None:
        with pool.use("primary", "model", lambda: model):
            entered.set()
            assert release.wait(5)

    def second() -> None:
        attempted.set()
        with pool.use("primary", "model", lambda: model):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(first)
        assert entered.wait(5)
        two = executor.submit(second)
        assert attempted.wait(5)
        assert not second_entered.wait(0.05)
        release.set()
        one.result(timeout=5)
        two.result(timeout=5)
    assert second_entered.is_set()
    assert len(pool.infos()) == 1


def test_new_model_settings_keep_old_multiwindow_task_then_retire(temp_db: None) -> None:
    pool = ModelPool()
    old_model = object()
    with settings_scope():
        with pool.use("primary", "model", lambda: old_model):
            pass
        save_configuration(ConfigurationChange(values={"asr_primary_device": "cuda"}))
        assert pool.cleanup_idle() == 0
        with pool.use("primary", "model", lambda: object()) as again:
            assert again is old_model
    assert pool.cleanup_idle() == 1
    with settings_scope():
        with pool.use("primary", "model", lambda: object()) as new:
            assert new is not old_model


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("asr_primary", "whisper"),
        ("asr_sensevoice", False),
        ("asr_sensevoice_enabled", False),
        ("asr_funasr_review", False),
        ("asr_fallback_whisper", False),
    ],
)
def test_chain_change_retires_resident_model_after_old_task_exits(temp_db: None, key: str, value: str | bool) -> None:
    save_configuration(
        ConfigurationChange(
            values={
                "asr_primary": "funasr_nano",
                "asr_sensevoice": True,
                "asr_sensevoice_enabled": True,
                "asr_funasr_review": True,
                "asr_fallback_whisper": True,
                "asr_primary_keep_loaded": True,
            }
        )
    )
    pool = ModelPool()
    old_model = object()
    with settings_scope():
        with pool.use("primary", "old-chain", lambda: old_model):
            pass
        save_configuration(ConfigurationChange(values={key: value}))
        assert pool.cleanup_idle() == 0
        with pool.use("primary", "old-chain", object) as reused:
            assert reused is old_model
    assert pool.cleanup_idle() == 1
    assert pool.infos() == []


def test_policy_change_preserves_loaded_model_and_applies_idle_rule(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis import model_pool as module

    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    pool = ModelPool()
    model = object()
    with pool.use("primary", "model", lambda: model):
        pass
    save_configuration(
        ConfigurationChange(
            values={
                "asr_primary_max_concurrency": 2,
                "asr_primary_keep_loaded": False,
                "asr_model_idle_unload_seconds": 20,
            }
        )
    )
    assert pool.cleanup_idle() == 0
    with pool.use("primary", "model", lambda: object()) as reused:
        assert reused is model
    clock[0] = 31.0
    assert pool.cleanup_idle() == 1


def test_loading_failure_does_not_consume_model_slot() -> None:
    pool = ModelPool()

    def failed() -> object:
        raise OSError("missing model")

    with pytest.raises(OSError, match="missing model"):
        with pool.use("auxiliary", "model", failed):
            pytest.fail("failed model cannot be acquired")
    assert pool.infos() == []
    with pool.use("auxiliary", "model", object):
        assert pool.infos()[0]["is_loaded"] is True


def test_whisper_lazy_output_is_consumed_under_model_lease(monkeypatch: MonkeyPatch) -> None:
    from app.analysis.model_pool import model_pool
    from app.analysis.transcription import backends

    def segments() -> Iterator[SimpleNamespace]:
        assert model_pool.infos()[0]["active_users"] == 1
        yield SimpleNamespace(start=0, end=1, text="可用语音", words=[], avg_logprob=-0.1)

    model = SimpleNamespace(transcribe=lambda *_args, **_kwargs: (segments(), SimpleNamespace(language="zh")))
    monkeypatch.setattr(backends, "_probe_audio_duration", lambda _path: 1)
    monkeypatch.setattr(backends.FasterWhisperBackend, "_load_model", lambda _self: model)
    result = backends.FasterWhisperBackend().transcribe("audio.wav")
    assert result.text == "可用语音"
    assert model_pool.infos()[0]["active_users"] == 0


@pytest.mark.parametrize("concurrency", [1, 2])
def test_pipeline_refreshes_global_and_thread_cache_next_task(temp_db: None, concurrency: int) -> None:
    from app.analysis.transcription.pipeline import get_task_pipeline

    save_configuration(ConfigurationChange(values={"asr_task_max_concurrency": concurrency}))
    with settings_scope():
        first = get_task_pipeline()
        save_configuration(ConfigurationChange(values={"asr_review_risk_threshold": 0.9}))
        assert get_task_pipeline() is first
    with settings_scope():
        next_task = get_task_pipeline()
        assert next_task is not first
        assert next_task._review_risk_threshold == 0.9


def test_llm_same_prefix_rotation_does_not_close_active_client(monkeypatch: MonkeyPatch) -> None:
    import sys

    from app.analysis import llm
    from app.analysis.llm_providers import LLMProvider

    clients: list[FakeClient] = []
    entered = Event()
    release = Event()

    class FakeClient:
        def __init__(self, *, api_key: str, base_url: str | None) -> None:
            self.api_key = api_key
            self.closed = False
            clients.append(self)

        def close(self) -> None:
            self.closed = True

    module = ModuleType("openai")
    module.OpenAI = FakeClient
    monkeypatch.setitem(sys.modules, "openai", module)
    monkeypatch.setattr(llm, "_client_cache", {})
    first = LLMProvider("rotation", "first", "https://example.invalid", "samehead-old", "test")
    second = LLMProvider("rotation", "second", "https://example.invalid", "samehead-new", "test")

    def completion(client: FakeClient, *_args: object) -> dict[str, object]:
        entered.set()
        assert release.wait(5)
        assert not client.closed
        return {"choices": [{"message": {"content": "success"}}]}

    monkeypatch.setattr(llm, "_create_completion", completion)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(llm._complete, first, "hello", 10)
        assert entered.wait(5)
        replacement = llm._get_client(second)
        assert replacement.api_key == "samehead-new"
        assert len(clients) == 2
        assert not clients[0].closed
        release.set()
        assert pending.result(timeout=5) == "success"
    assert clients[0].closed
    assert not clients[1].closed
