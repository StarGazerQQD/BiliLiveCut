"""Runtime 目录布局管理 — 原子安装、current.json、并发锁。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

RELEASE_VERSION = "0.1.14.7-alpha"
SOURCE_COMMIT_SHORT = "731a31c"
RELEASE_ID = f"{RELEASE_VERSION}+{SOURCE_COMMIT_SHORT}"

# 持久数据目录（不随 Release 删除）
DATA_DIRS = ["data", "storage", "models", "vendor", "bin", "logs"]


def get_app_root() -> Path:
    """获取 Portable 应用根目录（EXE 所在目录或工作目录）。

    :returns: 应用根目录。
    """
    # PyInstaller one-file: sys._MEIPASS 是临时解压目录，exe 在 sys.executable 的父目录
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def get_runtime_root(app_root: Path | None = None) -> Path:
    """获取 Runtime 根目录。

    :param app_root: 应用根目录。
    :returns: Runtime 目录。
    """
    if app_root is None:
        app_root = get_app_root()
    return app_root / "runtime"


def get_releases_dir(app_root: Path | None = None) -> Path:
    """获取 releases 目录。

    :param app_root: 应用根目录。
    :returns: releases 目录。
    """
    return get_runtime_root(app_root) / "releases"


def get_staging_dir(app_root: Path | None = None) -> Path:
    """获取 staging 目录（原子安装用）。

    :param app_root: 应用根目录。
    :returns: staging 目录。
    """
    return get_runtime_root(app_root) / "staging"


def get_release_dir(release_id: str | None = None, app_root: Path | None = None) -> Path:
    """获取指定 Release 的安装目录。

    :param release_id: Release ID，默认当前版本。
    :param app_root: 应用根目录。
    :returns: Release 目录。
    """
    if release_id is None:
        release_id = RELEASE_ID
    return get_releases_dir(app_root) / release_id


def get_current_json(app_root: Path | None = None) -> Path:
    """获取 current.json 路径。

    :param app_root: 应用根目录。
    :returns: current.json 路径。
    """
    return get_runtime_root(app_root) / "current.json"


def read_current(app_root: Path | None = None) -> dict[str, Any] | None:
    """读取当前激活的 Release 信息。

    :param app_root: 应用根目录。
    :returns: current.json 内容，不存在返回 None。
    """
    path = get_current_json(app_root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_current(info: dict[str, Any], app_root: Path | None = None) -> None:
    """原子写入 current.json。

    :param info: Release 信息。
    :param app_root: 应用根目录。
    """
    path = get_current_json(app_root)
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)

    content = json.dumps(info, ensure_ascii=False, indent=2)
    tmp.write_text(content, encoding="utf-8")
    # flush + fsync
    os.replace(str(tmp), str(path))


def get_active_source_dir(app_root: Path | None = None) -> Path | None:
    """获取当前激活的源码目录。

    :param app_root: 应用根目录。
    :returns: 源码目录路径，不存在返回 None。
    """
    current_info = read_current(app_root)
    if current_info is None:
        return None

    release_id = current_info.get("release_id", RELEASE_ID)
    release_dir = get_release_dir(release_id, app_root)
    if release_dir.exists():
        return release_dir
    return None


def install_release(
    payload_zip_path: Path,
    manifest: dict[str, Any],
    app_root: Path | None = None,
) -> dict[str, Any]:
    """原子安装 Payload 到 releases 目录。

    :param payload_zip_path: Payload ZIP 路径。
    :param manifest: Manifest 字典。
    :param app_root: 应用根目录。
    :returns: 安装信息。
    """
    import zipfile

    from ..payload.manifest import validate_manifest

    if app_root is None:
        app_root = get_app_root()

    # 验证 Manifest
    errors = validate_manifest(manifest, payload_zip_path)
    if errors:
        raise RuntimeError(f"Manifest 验证失败: {errors}")

    release_id = RELEASE_ID
    staging = get_staging_dir(app_root)
    release = get_release_dir(release_id, app_root)

    # 如果已安装且有效，跳过
    if release.exists() and (release / "app" / "cli.py").exists():
        _logger.info("Release 已安装: %s", release_id)
        return {
            "release_id": release_id,
            "installed": True,
            "already_exists": True,
            "release_dir": str(release),
        }

    # 清理旧的 staging
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: 安全解压到 staging
        with zipfile.ZipFile(payload_zip_path) as zf:
            for member in zf.namelist():
                if member.startswith("/") or ".." in member.split("/"):
                    raise RuntimeError(f"ZIP 包含不安全路径: {member}")
                if ":" in member:
                    raise RuntimeError(f"ZIP 包含盘符: {member}")

                target = (staging / member).resolve()
                if not str(target).startswith(str(staging.resolve())):
                    raise RuntimeError(f"ZIP 路径越界: {member}")

                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(member))

        # Step 2: 验证关键文件
        for path in ["app/cli.py", "pyproject.toml"]:
            if not (staging / path).exists():
                raise RuntimeError(f"Release 缺少关键文件: {path}")

        # Step 3: 验证版本
        init_py = staging / "app" / "__init__.py"
        if init_py.exists():
            content = init_py.read_text(encoding="utf-8")
            if RELEASE_VERSION not in content:
                raise RuntimeError(f"版本不匹配: 期望 {RELEASE_VERSION}")

        # Step 4: 原子 rename 到 releases
        release.parent.mkdir(parents=True, exist_ok=True)
        if release.exists():
            shutil.rmtree(release)
        os.replace(str(staging), str(release))

        # Step 5: 写入 current.json
        current_info: dict[str, Any] = {
            "release_id": release_id,
            "release_version": RELEASE_VERSION,
            "source_commit": manifest.get("source_commit", ""),
            "source_commit_short": SOURCE_COMMIT_SHORT,
            "builder_commit": manifest.get("builder_commit", ""),
            "payload_sha256": manifest.get("payload_sha256", ""),
            "installed_at": datetime.now(UTC).isoformat(),
        }
        write_current(current_info, app_root)

        _logger.info("Release 安装完成: %s → %s", release_id, release)
        return {
            "release_id": release_id,
            "installed": True,
            "release_dir": str(release),
            "current_info": current_info,
        }

    except Exception:
        # 清理 staging
        if staging.exists():
            shutil.rmtree(staging)
        raise


def ensure_data_dirs(app_root: Path | None = None) -> None:
    """确保持久数据目录存在。

    :param app_root: 应用根目录。
    """
    if app_root is None:
        app_root = get_app_root()

    for d in DATA_DIRS:
        (app_root / d).mkdir(parents=True, exist_ok=True)


def create_env_from_template(app_root: Path | None = None) -> bool:
    """如果 .env 不存在，从 Payload 模板创建。

    :param app_root: 应用根目录。
    :returns: True 表示创建了新文件。
    """
    if app_root is None:
        app_root = get_app_root()

    env_path = app_root / ".env"
    if env_path.exists():
        return False

    source_dir = get_active_source_dir(app_root)
    template = None
    if source_dir:
        template_path = source_dir / ".env.example"
        if template_path.exists():
            template = template_path.read_text(encoding="utf-8")

    if template:
        env_path.write_text(template, encoding="utf-8")
        _logger.info(".env 已从模板创建: %s", env_path)
        return True

    return False
