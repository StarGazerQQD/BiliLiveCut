"""Portable Launcher 与 Web 设置页共享的持久化启动配置。"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LAUNCHER_CONFIG_SCHEMA = 1
DEFAULT_WEB_PORT = 8000
WEB_PORT_ENV = "BLC_WEB_PORT"
APP_ROOT_ENV = "BLC_APP_ROOT"


class LauncherConfigError(ValueError):
    """启动器配置无效或无法安全写入。"""


@dataclass(frozen=True, slots=True)
class LauncherConfig:
    """当前启动器配置及只读诊断信息。"""

    web_port: int = DEFAULT_WEB_PORT
    warning: str | None = None


def validate_web_port(value: object) -> int:
    """校验并返回 TCP 端口。

    :param value: 待校验值。
    :returns: ``1..65535`` 范围内的端口。
    :raises LauncherConfigError: 值不是合法整数端口时。
    """
    if isinstance(value, bool):
        raise LauncherConfigError("Web 端口必须是 1 到 65535 之间的整数。")
    if isinstance(value, float) and not value.is_integer():
        raise LauncherConfigError("Web 端口必须是 1 到 65535 之间的整数。")
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise LauncherConfigError("Web 端口必须是 1 到 65535 之间的整数。") from exc
    if not 1 <= port <= 65535:
        raise LauncherConfigError("Web 端口必须是 1 到 65535 之间的整数。")
    return port


def launcher_config_path(app_root: Path) -> Path:
    """返回升级保留的 Launcher 配置路径。"""
    return app_root.resolve() / "config" / "launcher.json"


def runtime_app_root(environ: Mapping[str, str] | None = None) -> Path:
    """返回当前应用根目录，Portable 由 Launcher 显式注入。"""
    env = os.environ if environ is None else environ
    configured = env.get(APP_ROOT_ENV, "").strip()
    return Path(configured).expanduser().resolve() if configured else Path.cwd().resolve()


def _decode_launcher_config(raw: object) -> int:
    """解析当前唯一的 Launcher 配置 schema。"""
    if not isinstance(raw, dict):
        raise LauncherConfigError("配置根节点必须是 JSON 对象")
    if set(raw) != {"schema", "web_port"}:
        raise LauncherConfigError("配置字段必须且只能包含 schema 与 web_port")
    if raw["schema"] != LAUNCHER_CONFIG_SCHEMA:
        raise LauncherConfigError(f"不支持的配置 schema: {raw['schema']!r}")
    return validate_web_port(raw["web_port"])


def load_launcher_config(
    app_root: Path,
    *,
    warn: Callable[[str], None] | None = None,
) -> LauncherConfig:
    """读取启动器配置；损坏时只告警并回退，不改写原文件。"""
    path = launcher_config_path(app_root)
    if not path.exists():
        return LauncherConfig()
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        return LauncherConfig(web_port=_decode_launcher_config(payload))
    except (OSError, UnicodeError, json.JSONDecodeError, LauncherConfigError) as exc:
        message = f"Launcher 配置损坏，已回退到端口 {DEFAULT_WEB_PORT} 且未覆盖原文件: {path} ({exc})"
        if warn is not None:
            warn(message)
        return LauncherConfig(warning=message)


def save_launcher_config(app_root: Path, *, web_port: object) -> LauncherConfig:
    """原子保存启动器配置，并拒绝覆盖已有损坏文件。"""
    port = validate_web_port(web_port)
    path = launcher_config_path(app_root)
    if path.exists():
        existing = load_launcher_config(app_root)
        if existing.warning is not None:
            raise LauncherConfigError("现有 launcher.json 已损坏；为避免数据丢失，本次保存已拒绝。")

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": LAUNCHER_CONFIG_SCHEMA, "web_port": port}
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise LauncherConfigError(f"无法保存 Launcher 配置: {exc}") from exc
    return LauncherConfig(web_port=port)


def resolve_web_port(app_root: Path, *, explicit_port: object | None = None) -> int:
    """按“显式参数 > 持久化配置 > 8000”解析启动端口。"""
    if explicit_port is not None:
        return validate_web_port(explicit_port)
    return load_launcher_config(app_root).web_port


def current_web_port(environ: Mapping[str, str] | None = None) -> int:
    """读取本次进程由 Launcher 注入的真实监听端口。"""
    env = os.environ if environ is None else environ
    raw = env.get(WEB_PORT_ENV, "").strip()
    return validate_web_port(raw) if raw else DEFAULT_WEB_PORT
