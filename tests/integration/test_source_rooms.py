from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlmodel import Session, select
from typer.testing import CliRunner

from app.db.entities import AppSetting, LiveRoom, RecordingSession
from app.db.session import get_session
from app.plugins.live_source import (
    RoomSnapshot,
    SourceDescriptor,
    SourceInvalidInput,
    SourceRoom,
    StreamPreference,
    StreamSpec,
)
from app.sources.registry import SourceRegistry, source_registry
from app.sources.rooms import register_room, room_source


class ExternalSource:
    descriptor = SourceDescriptor(platform="external", name="外部来源", domains=("external.invalid", "short.invalid"))

    async def resolve_room(self, value: str) -> SourceRoom:
        return SourceRoom(platform="external", source_id="123", canonical_url="https://external.invalid/123")

    async def get_room_info(self, room: SourceRoom) -> RoomSnapshot:
        return RoomSnapshot(status="offline", title="外部标题", uploader_name="外部主播")

    async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        return []

    async def aclose(self) -> None:
        pass


@pytest.fixture
def isolated_sources(monkeypatch: MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(source_registry, "_entries", SourceRegistry()._entries)
    yield


async def test_parallel_alias_registration_is_idempotent_and_preserves_platforms(
    temp_db: None, isolated_sources: None
) -> None:
    with get_session() as db:
        bili = LiveRoom(platform="bilibili", room_id=123, input_url="123", authorized=True)
        db.add(bili)
        db.flush()
        bili_id = bili.id
    source_registry.register_many("external-plugin", [ExternalSource()])
    results = await asyncio.gather(
        *[
            register_room(value, True, "external")
            for value in ("123", "https://short.invalid/abc", "https://external.invalid/123")
        ]
    )
    assert len({room.id for room in results}) == 1
    room = results[0]
    assert room.id != bili_id and room.room_id is None and room.platform == "external"
    assert not any((room.auto_record, room.auto_analyze, room.auto_render, room.auto_approve, room.auto_upload))
    with get_session() as db:
        assert len(db.exec(select(LiveRoom)).all()) == 2
        assert room_source(db.get(LiveRoom, bili_id), db).platform == "bilibili"
        assert room_source(room, db).source_id == "123"
        settings_rows = db.exec(select(AppSetting).where(AppSetting.key.startswith("source_"))).all()
        assert len(settings_rows) == 2
    await source_registry.unregister_owner("external-plugin")
    # 卸载不改身份/历史，重新注册后仍命中相同 DB 房间。
    source_registry.register_many("external-plugin", [ExternalSource()])
    assert (await register_room("123", True, "external")).id == room.id
    await source_registry.unregister_owner("external-plugin")


async def test_existing_bilibili_room_keeps_history_and_schema(
    temp_db: None, isolated_sources: None, monkeypatch: MonkeyPatch
) -> None:
    from app.db.schema import SchemaMeta, compute_schema_fingerprint, validate_schema
    from app.sources.bilibili.client import BilibiliLiveClient, RoomInfo

    async def info(self: BilibiliLiveClient, value: str, *, include_detail: bool = True) -> RoomInfo:
        return RoomInfo(123, 1, 99, 0, title="新标题")

    monkeypatch.setattr(BilibiliLiveClient, "get_room_info", info)
    with get_session() as db:
        original = LiveRoom(room_id=123, input_url="1", authorized=True, auto_analyze=True)
        db.add(original)
        db.flush()
        db.add(RecordingSession(room_id=original.id))
        meta = db.get(SchemaMeta, 1).model_dump()
        fingerprint = compute_schema_fingerprint()
        original_id = original.id
    room = await register_room("1", True)
    assert room.id == original_id and room.auto_analyze and room.title == "新标题"
    with get_session() as db:
        assert db.exec(select(RecordingSession)).one().room_id == original_id
        assert db.get(SchemaMeta, 1).model_dump() == meta
        assert compute_schema_fingerprint() == fingerprint
        assert validate_schema()


async def test_registration_failure_rolls_back_room_and_both_bindings(
    temp_db: None, isolated_sources: None, monkeypatch: MonkeyPatch
) -> None:
    source_registry.register_many("external-plugin", [ExternalSource()])
    original_add = Session.add

    def add(self: Session, instance: object, *, _warn: bool = True) -> None:
        if isinstance(instance, AppSetting) and instance.key.startswith("source_room:"):
            raise RuntimeError("模拟反向索引写入失败")
        original_add(self, instance, _warn=_warn)

    with monkeypatch.context() as context:
        context.setattr(Session, "add", add)
        with pytest.raises(RuntimeError, match="反向索引"):
            await register_room("https://external.invalid/a", True)
    with get_session() as db:
        assert db.exec(select(LiveRoom)).all() == []
        assert db.exec(select(AppSetting).where(AppSetting.key.startswith("source_"))).all() == []
    assert (await register_room("https://external.invalid/a", True)).id is not None
    await source_registry.unregister_owner("external-plugin")


async def test_corrupt_identity_is_preserved_and_not_reassigned(temp_db: None, isolated_sources: None) -> None:
    source_registry.register_many("external-plugin", [ExternalSource()])
    room = await register_room("https://external.invalid/a", True)
    with get_session() as db:
        row = db.get(AppSetting, f"source_room:{room.id}")
        row.value = '{"version":999}'
        db.add(row)
    with pytest.raises(SourceInvalidInput, match="损坏"):
        await register_room("https://external.invalid/a", True)
    with get_session() as db:
        assert len(db.exec(select(LiveRoom)).all()) == 1
        assert db.get(AppSetting, f"source_room:{room.id}").value == '{"version":999}'
    await source_registry.unregister_owner("external-plugin")


async def test_canonical_url_cannot_be_reassigned_to_different_identity(temp_db: None, isolated_sources: None) -> None:
    class ChangingIdentity(ExternalSource):
        async def resolve_room(self, value: str) -> SourceRoom:
            return SourceRoom(platform="external", source_id=value, canonical_url="https://external.invalid/canonical")

    source_registry.register_many("external-plugin", [ChangingIdentity()])
    original = await register_room("first-id", True, "external")
    with pytest.raises(SourceInvalidInput, match="规范地址"):
        await register_room("second-id", True, "external")
    assert room_source(original).source_id == "first-id"
    with get_session() as db:
        assert len(db.exec(select(LiveRoom)).all()) == 1
    await source_registry.unregister_owner("external-plugin")


async def test_missing_forward_binding_is_not_silently_recreated(temp_db: None, isolated_sources: None) -> None:
    source_registry.register_many("external-plugin", [ExternalSource()])
    room = await register_room("123", True, "external")
    with get_session() as db:
        index = db.exec(select(AppSetting).where(AppSetting.key.startswith("source_identity:"))).one()
        db.delete(index)
    with pytest.raises(SourceInvalidInput, match="正向索引"):
        await register_room("123", True, "external")
    with get_session() as db:
        assert len(db.exec(select(LiveRoom)).all()) == 1
        assert db.get(AppSetting, f"source_room:{room.id}") is not None
        assert db.exec(select(AppSetting).where(AppSetting.key.startswith("source_identity:"))).all() == []
    await source_registry.unregister_owner("external-plugin")


async def test_partial_bilibili_binding_is_not_mistaken_for_legacy_room(
    temp_db: None, isolated_sources: None, monkeypatch: MonkeyPatch
) -> None:
    from app.sources.bilibili.client import BilibiliLiveClient, RoomInfo

    async def info(self: BilibiliLiveClient, value: str, *, include_detail: bool = True) -> RoomInfo:
        return RoomInfo(123, 1, 99, 0)

    monkeypatch.setattr(BilibiliLiveClient, "get_room_info", info)
    room = await register_room("1", True)
    with get_session() as db:
        row = db.get(AppSetting, f"source_room:{room.id}")
        db.delete(row)
    with pytest.raises(SourceInvalidInput, match="反向索引"):
        room_source(room)
    with pytest.raises(SourceInvalidInput, match="反向索引"):
        await register_room("1", True)


def write_plugin(root: Path) -> Path:
    directory = root / "external-source"
    directory.mkdir()
    (directory / "plugin.json").write_text(
        json.dumps(
            {
                "id": "external-source",
                "name": "外部来源",
                "version": "1",
                "api_version": "1",
                "live_source_api_version": "1",
                "capabilities": ["live_source"],
                "entrypoint": "main.py:Plugin",
            }
        ),
        encoding="utf-8",
    )
    (directory / "main.py").write_text(
        """from app.plugins import BasePlugin
from app.plugins.live_source import SourceDescriptor, SourceRoom, RoomSnapshot
class Source:
    descriptor = SourceDescriptor(platform="external", name="外部来源", domains=("external.invalid", "short.invalid"))
    def __init__(self, path): self.path=path
    async def resolve_room(self, value):
        return SourceRoom(platform="external", source_id="room-ab", canonical_url="https://external.invalid/room-ab")
    async def get_room_info(self, room): return RoomSnapshot(status="offline", title="标题", uploader_name="主播")
    async def get_streams(self, room, preference): return []
    async def aclose(self): (self.path / "closed").write_text("yes")
class Plugin(BasePlugin):
    def on_enable(self, context): context.register_live_source(Source(context.plugin_dir))
""",
        encoding="utf-8",
    )
    return directory


def test_cli_web_share_loader_identity_and_missing_source_display(
    temp_db: None, isolated_sources: None, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from app import cli as cli_module
    from app.cli import app as cli
    from app.core.settings_store import set_bool
    from app.plugins import manager as manager_module
    from app.plugins import runtime
    from app.plugins.manager import PluginManager
    from app.web.main import app
    from app.web.routers import plugins as plugin_routes

    directory = write_plugin(tmp_path)
    manager = PluginManager(tmp_path, registry=source_registry)
    monkeypatch.setattr(manager_module, "plugin_manager", manager)
    monkeypatch.setattr(runtime, "plugin_manager", manager)
    monkeypatch.setattr(plugin_routes, "plugin_manager", manager)
    monkeypatch.setattr("app.web.main._rate_buckets", {})
    # CliRunner 的临时输出流会关闭，不把它注册到进程级异步日志 sink。
    monkeypatch.setattr(cli_module, "setup_logging", lambda: None)
    set_bool("plugin.external-source.enabled", True)
    runner = CliRunner()
    added = runner.invoke(cli, ["add-room", "room-ab", "--platform", "external", "--authorize"])
    assert added.exit_code == 0, added.output
    assert "platform=external" in added.output and "source_id=room-ab" in added.output
    assert (directory / "closed").is_file() and not source_registry.available("external")
    with get_session() as db:
        original = db.exec(select(LiveRoom)).one()
        db_id = original.id
    checked = runner.invoke(cli, ["check", "https://short.invalid/link"])
    assert checked.exit_code == 0 and "live_status=offline" in checked.output
    with TestClient(app) as client:
        response = client.post("/api/rooms", json={"url": "https://short.invalid/link", "authorized": True})
        assert response.status_code == 200, response.text
        assert response.json()["id"] == db_id
        assert response.json()["platform"] == "external" and response.json()["room_id"] is None
        assert "external" in {item["platform"] for item in client.get("/api/live-sources").json()}
        disabled = client.patch("/api/plugins/external-source", json={"enabled": False})
        assert disabled.status_code == 200, disabled.text
        room = next(item for item in client.get("/api/dashboard").json()["rooms"] if item["id"] == db_id)
        assert room["source_available"] is False and room["source_id"] == "room-ab"
        assert "不可用" in room["source_error"]
        unsupported = client.post("/api/rooms", json={"url": "https://short.invalid/a", "authorized": True})
        assert unsupported.status_code == 400 and "启用" in unsupported.json()["detail"]
    with get_session() as db:
        assert db.get(LiveRoom, db_id) is not None


def test_cli_close_failure_does_not_display_plugin_traceback(
    temp_db: None, isolated_sources: None, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from app import cli as cli_module
    from app.core.settings_store import set_bool
    from app.plugins import runtime
    from app.plugins.manager import PluginManager

    directory = write_plugin(tmp_path)
    path = directory / "main.py"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '(self.path / "closed").write_text("yes")', 'raise RuntimeError("Cookie=secret-token")'
        ),
        encoding="utf-8",
    )
    manager = PluginManager(tmp_path, registry=source_registry)
    monkeypatch.setattr(runtime, "plugin_manager", manager)
    monkeypatch.setattr(cli_module, "setup_logging", lambda: None)
    set_bool("plugin.external-source.enabled", True)
    result = CliRunner().invoke(cli_module.app, ["check", "room-ab", "--platform", "external"])
    assert result.exit_code == 1
    assert "secret-token" not in result.output and "收尾" in result.output

    async def close(source: object) -> None:
        pass

    sys.modules["_bililivecut_plugin_external_source"].Source.aclose = close
    asyncio.run(manager.stop())
