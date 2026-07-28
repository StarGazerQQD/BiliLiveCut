from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.plugins.manager import PluginManager, PluginStateError, PluginValidationError


def _write_plugin(root: Path, plugin_id: str = "demo") -> Path:
    directory = root / plugin_id
    directory.mkdir(parents=True)
    manifest = {
        "id": plugin_id,
        "name": "测试插件",
        "version": "1.0.0",
        "api_version": "1",
        "entrypoint": "main.py:Plugin",
        "description": "用于验证宿主插件契约",
        "settings_page": True,
    }
    (directory / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (directory / "main.py").write_text(
        """from app.plugins import BasePlugin, PluginSetting

class Plugin(BasePlugin):
    settings_schema = (
        PluginSetting(key="mode", label="模式", kind="select", choices=("safe", "fast"), default="safe"),
        PluginSetting(key="limit", label="上限", kind="number", default=2, minimum=1, maximum=5),
        PluginSetting(key="token", label="令牌", kind="password", default=""),
    )

    def on_enable(self, context):
        self.context = context
        (context.plugin_dir / "enabled.txt").write_text("enabled", encoding="utf-8")

    def on_disable(self):
        (self.context.plugin_dir / "disabled.txt").write_text("disabled", encoding="utf-8")
""",
        encoding="utf-8",
    )
    return directory


@pytest.mark.asyncio
async def test_scan_does_not_execute_until_explicit_enable(temp_db: None, tmp_path: Path) -> None:
    plugin_dir = _write_plugin(tmp_path)
    manager = PluginManager(tmp_path)

    await manager.start()
    payload = manager.list_payload()
    assert payload["scan_errors"] == []
    assert payload["plugins"][0]["enabled"] is False
    assert payload["plugins"][0]["loaded"] is False
    assert not (plugin_dir / "enabled.txt").exists()

    enabled = await manager.set_enabled("demo", True)
    assert enabled["enabled"] is True
    assert enabled["loaded"] is True
    assert (plugin_dir / "enabled.txt").read_text(encoding="utf-8") == "enabled"

    await manager.set_enabled("demo", False)
    assert (plugin_dir / "disabled.txt").read_text(encoding="utf-8") == "disabled"
    await manager.stop()


@pytest.mark.asyncio
async def test_plugin_settings_are_validated_namespaced_and_secrets_are_hidden(
    temp_db: None,
    tmp_path: Path,
) -> None:
    _write_plugin(tmp_path)
    manager = PluginManager(tmp_path)
    await manager.start()
    await manager.set_enabled("demo", True)

    initial = manager.settings_payload("demo")
    assert [field["key"] for field in initial["fields"]] == ["mode", "limit", "token"]
    updated = manager.update_settings("demo", {"mode": "fast", "limit": 4, "token": "secret"})
    values = {field["key"]: field for field in updated["fields"]}
    assert values["mode"]["value"] == "fast"
    assert values["limit"]["value"] == 4.0
    assert values["token"]["value"] == ""
    assert values["token"]["configured"] is True

    manager.update_settings("demo", {"token": ""})
    assert manager._read_setting("demo", "token", None) == "secret"  # noqa: SLF001
    with pytest.raises(PluginValidationError, match="允许选项"):
        manager.update_settings("demo", {"mode": "unsafe"})
    with pytest.raises(PluginValidationError, match="不能大于"):
        manager.update_settings("demo", {"limit": 10})
    with pytest.raises(PluginValidationError, match="未知设置项"):
        manager.update_settings("demo", {"other": True})
    await manager.stop()


@pytest.mark.asyncio
async def test_invalid_manifest_is_reported_without_import(temp_db: None, tmp_path: Path) -> None:
    directory = tmp_path / "bad-name"
    directory.mkdir()
    (directory / "plugin.json").write_text(
        json.dumps(
            {
                "id": "different-name",
                "name": "坏清单",
                "version": "1",
                "api_version": "1",
                "entrypoint": "../outside.py:Plugin",
            }
        ),
        encoding="utf-8",
    )
    (directory / "main.py").write_text("raise RuntimeError('不得执行')", encoding="utf-8")
    manager = PluginManager(tmp_path)

    await manager.start()
    payload = manager.list_payload()
    assert payload["plugins"] == []
    assert payload["scan_errors"][0]["directory"] == "bad-name"
    assert "entrypoint" in payload["scan_errors"][0]["error"]
    await manager.stop()


@pytest.mark.asyncio
async def test_enabled_state_is_restored_on_next_manager_start(temp_db: None, tmp_path: Path) -> None:
    _write_plugin(tmp_path)
    first = PluginManager(tmp_path)
    await first.start()
    await first.set_enabled("demo", True)
    await first.stop()

    second = PluginManager(tmp_path)
    await second.start()
    descriptor = second.descriptor("demo")
    assert descriptor["enabled"] is True
    assert descriptor["loaded"] is True
    await second.stop()


@pytest.mark.asyncio
async def test_failed_enable_runs_cleanup_and_keeps_plugin_disabled(temp_db: None, tmp_path: Path) -> None:
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "plugin.json").write_text(
        json.dumps(
            {
                "id": "broken",
                "name": "故障插件",
                "version": "1",
                "api_version": "1",
                "entrypoint": "main.py:Plugin",
            }
        ),
        encoding="utf-8",
    )
    (directory / "main.py").write_text(
        """from app.plugins import BasePlugin

class Plugin(BasePlugin):
    def on_enable(self, context):
        self.directory = context.plugin_dir
        raise RuntimeError("初始化失败")

    def on_disable(self):
        (self.directory / "rolled-back.txt").write_text("ok", encoding="utf-8")
""",
        encoding="utf-8",
    )
    manager = PluginManager(tmp_path)
    await manager.start()

    with pytest.raises(PluginStateError, match="初始化失败"):
        await manager.set_enabled("broken", True)
    descriptor = manager.descriptor("broken")
    assert descriptor["enabled"] is False
    assert descriptor["loaded"] is False
    assert (directory / "rolled-back.txt").read_text(encoding="utf-8") == "ok"
    await manager.stop()
