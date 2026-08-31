"""Launcher 持久化端口配置回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.launcher_settings import (
    DEFAULT_WEB_PORT,
    LauncherConfigError,
    current_web_port,
    launcher_config_path,
    load_launcher_config,
    resolve_web_port,
    save_launcher_config,
)


def test_missing_launcher_config_uses_default_without_creating_file(tmp_path: Path) -> None:
    """首次启动默认使用 8000，读取动作不应制造配置文件。"""
    assert load_launcher_config(tmp_path).web_port == DEFAULT_WEB_PORT
    assert not launcher_config_path(tmp_path).exists()


def test_launcher_config_round_trip_and_explicit_precedence(tmp_path: Path) -> None:
    """持久化端口可重读，显式值始终优先。"""
    saved = save_launcher_config(tmp_path, web_port=8080)

    assert saved.web_port == 8080
    assert resolve_web_port(tmp_path) == 8080
    assert resolve_web_port(tmp_path, explicit_port=9000) == 9000
    assert json.loads(launcher_config_path(tmp_path).read_text(encoding="utf-8")) == {
        "schema": 1,
        "web_port": 8080,
    }


@pytest.mark.parametrize("value", [0, 65536, True, 8080.5, "not-a-port"])
def test_invalid_launcher_port_is_rejected_without_mutation(tmp_path: Path, value: object) -> None:
    """非法端口不能创建或改写配置。"""
    save_launcher_config(tmp_path, web_port=8080)
    before = launcher_config_path(tmp_path).read_bytes()

    with pytest.raises(LauncherConfigError, match="1 到 65535"):
        save_launcher_config(tmp_path, web_port=value)

    assert launcher_config_path(tmp_path).read_bytes() == before


def test_corrupt_launcher_config_warns_and_is_not_overwritten(tmp_path: Path) -> None:
    """损坏 JSON 回退到 8000，读取和保存都不得覆盖现场。"""
    path = launcher_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    warnings: list[str] = []

    loaded = load_launcher_config(tmp_path, warn=warnings.append)

    assert loaded.web_port == DEFAULT_WEB_PORT
    assert loaded.warning is not None
    assert warnings and "未覆盖原文件" in warnings[0]
    with pytest.raises(LauncherConfigError, match="已损坏"):
        save_launcher_config(tmp_path, web_port=8080)
    assert path.read_text(encoding="utf-8") == "{broken"


def test_current_web_port_comes_from_process_environment() -> None:
    """当前端口只反映实际启动参数，不猜测磁盘配置。"""
    assert current_web_port({}) == DEFAULT_WEB_PORT
    assert current_web_port({"BLC_WEB_PORT": "8080"}) == 8080
