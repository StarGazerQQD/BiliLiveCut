"""pytest 公共夹具。

提供一个隔离的、基于临时目录的 SQLite 数据库,避免污染真实 storage。
"""

from __future__ import annotations

import sys
import wave
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from _pytest.monkeypatch import MonkeyPatch


@pytest.fixture()
def pcm_wav(tmp_path: Path) -> Path:
    """8.336 秒有效 PCM，供音频验证和模型边界替身共用。"""
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 133376)
    return path


@pytest.fixture
def bili_live_feed(monkeypatch: MonkeyPatch):
    """仅替换 Bilibili 开下播网络边界，测试仍使用真实事件监听生命周期。"""
    from app.plugins.live_source import DanmakuStateSink, LiveStateSink, SourceRoom
    from tests.source_fixtures import LiveEventFeed

    feed = LiveEventFeed()

    async def collect(_source: object, room: SourceRoom, emit: LiveStateSink, state: DanmakuStateSink) -> None:
        await feed.collect(room, emit, state)

    monkeypatch.setattr("app.sources.bilibili.source.BilibiliSource.collect_live_events", collect)
    return feed


@pytest.fixture(autouse=True)
def isolated_pipeline_shutdown() -> Iterator[None]:
    """每个测试模拟独立进程，避免 Web lifespan 关闭标记污染随后直接调用的流水线。"""
    from app.analysis.transcription.process_pool import get_process_pool, reset_process_pool
    from app.pipeline.lifecycle import shutdown_event

    reset_process_pool()
    shutdown_event.clear()
    yield
    get_process_pool().shutdown(0)
    shutdown_event.clear()


@pytest.fixture(autouse=True)
def isolated_model_pool() -> Iterator[None]:
    """每个测试清理边界替身的空闲模型，避免外部加载 mock 跨测试复用。"""
    from app.analysis.model_pool import model_pool

    model_pool.cleanup_idle(force=True)
    yield
    model_pool.cleanup_idle(force=True)


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: MonkeyPatch) -> Iterator[None]:
    """创建一个临时 SQLite 数据库并重建引擎。

    通过环境变量覆盖 ``DATABASE_URL`` 与 ``STORAGE_ROOT``,清空配置缓存后
    重新导入会话模块,确保引擎指向临时库。

    :param tmp_path: pytest 提供的临时目录。
    :param monkeypatch: 用于设置环境变量。
    :yields: 无返回值,仅在测试期间提供隔离环境。
    """
    # 每个隔离应用保留自己的限流窗口，不能消费上一测试的请求配额。
    web_main = sys.modules.get("app.web.main")
    if web_main is not None:
        monkeypatch.setattr(web_main, "_rate_buckets", {})
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("BLC_APP_ROOT", str(tmp_path))

    # 清空配置单例缓存,使新环境变量生效。
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    config_module.get_settings()

    # 重建数据库引擎以指向临时库。
    import importlib

    from app.db import session as session_module

    importlib.reload(session_module)
    session_module.init_db()

    yield
