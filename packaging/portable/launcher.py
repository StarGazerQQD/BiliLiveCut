"""BiliLiveCut Portable Launcher — 从 EXE 内置 Payload 释放源码并启动。

运行流程:
1. 检查持久 Runtime → 如已安装且完好，直接启动
2. 读取 EXE 内置 Payload (source_payload.zip + payload_manifest.json)
3. 校验 Payload SHA-256
4. 原子安装到 runtime/releases/<release-id>/
5. Python/依赖/FFmpeg 检测
6. 启动 app.cli serve
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
import webbrowser
import zipfile
from pathlib import Path
from typing import Any

# ── 常量 ──────────────────────────────────────────────────────────
APP_NAME = "BiliLiveCut"
VERSION = "V0.1.14.5 Alpha"
RELEASE_VERSION = "0.1.14.5-alpha"
SOURCE_COMMIT_SHORT = "74c21b4"
RELEASE_ID = f"{RELEASE_VERSION}+{SOURCE_COMMIT_SHORT}"

VENV_DIR = ".venv"
WHEELS_DIR = os.path.join("vendor", "wheels")

# 国内镜像
PIP_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
PIP_EXTRA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_TRUSTED_HOSTS = ["mirrors.aliyun.com", "pypi.tuna.tsinghua.edu.cn"]

# Whisper 模型
MODEL_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
MODEL_DIRNAME = "whisper-large-v3-turbo"
HF_MIRROR = "https://hf-mirror.com"

# FFmpeg
FFMPEG_WIN_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)


# ── 资源路径 ──────────────────────────────────────────────────────


def get_bundled_resource_path(rel: str) -> Path | None:
    """获取打包资源路径，兼容 PyInstaller 和普通运行。

    :param rel: 相对路径。
    :returns: 资源路径，不存在返回 None。
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent / "dist" / "payload"

    p = base / rel
    return p if p.exists() else None


def get_payload_zip() -> Path:
    """获取内嵌 Payload ZIP 路径。

    :returns: Payload ZIP 路径。
    :raises RuntimeError: 找不到时。
    """
    p = get_bundled_resource_path("source_payload.zip")
    if p is None:
        raise RuntimeError(
            "找不到内置 Payload (source_payload.zip)。"
            "请确保 EXE 已正确嵌入 Payload。"
        )
    return p


def get_payload_manifest() -> dict[str, Any]:
    """读取内嵌 Manifest。

    :returns: Manifest 字典。
    :raises RuntimeError: 找不到时。
    """
    p = get_bundled_resource_path("payload_manifest.json")
    if p is None:
        raise RuntimeError(
            "找不到内置 Manifest (payload_manifest.json)。"
        )
    return json.loads(p.read_text(encoding="utf-8"))


# ── Runtime 管理 ──────────────────────────────────────────────────


def get_app_root() -> Path:
    """获取 Portable 应用根目录。

    :returns: 根目录路径。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def get_releases_dir() -> Path:
    """获取 releases 目录。

    :returns: releases 目录。
    """
    return get_app_root() / "runtime" / "releases"


def get_current_release_dir() -> Path | None:
    """获取当前激活的 Release 目录。

    :returns: Release 目录，不存在返回 None。
    """
    current_path = get_app_root() / "runtime" / "current.json"
    if not current_path.exists():
        return None
    try:
        info = json.loads(current_path.read_text(encoding="utf-8"))
        rid = info.get("release_id", RELEASE_ID)
        d = get_releases_dir() / rid
        return d if d.exists() and (d / "app" / "cli.py").exists() else None
    except (json.JSONDecodeError, OSError):
        return None


def install_source_from_payload(app_root: Path) -> Path:
    """从内嵌 Payload 原子安装源码到 Runtime。

    :param app_root: 应用根目录。
    :returns: 已安装的 Release 目录。
    """
    import hashlib

    manifest = get_payload_manifest()
    zip_path = get_payload_zip()

    # 校验 Payload SHA-256
    hasher = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    actual_hash = hasher.hexdigest()
    expected_hash = manifest.get("payload_sha256", "")
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"Payload 哈希不匹配: actual={actual_hash[:16]} expected={expected_hash[:16]}"
        )

    # 验证版本和 Commit
    if manifest.get("release_version") != RELEASE_VERSION:
        raise RuntimeError(
            f"Payload 版本不匹配: {manifest.get('release_version')} != {RELEASE_VERSION}"
        )
    if manifest.get("source_commit_short") != SOURCE_COMMIT_SHORT:
        raise RuntimeError(
            f"Source Commit 不匹配: {manifest.get('source_commit_short')} != {SOURCE_COMMIT_SHORT}"
        )

    print(f"  Payload: v{RELEASE_VERSION} | Source: {SOURCE_COMMIT_SHORT} | SHA256: {actual_hash[:16]}")

    releases_dir = get_releases_dir()
    staging = get_app_root() / "runtime" / "staging"
    release_dir = releases_dir / RELEASE_ID

    # 清理旧 staging
    if staging.exists():
        shutil.rmtree(staging)

    try:
        staging.mkdir(parents=True, exist_ok=True)

        # 安全解压到 staging
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                if member.startswith("/") or ".." in member.split("/") or ":" in member:
                    raise RuntimeError(f"ZIP 包含不安全路径: {member}")

                target = (staging / member).resolve()
                if not str(target).startswith(str(staging.resolve())):
                    raise RuntimeError(f"ZIP 路径越界: {member}")

                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(member))

        # 验证关键文件
        for path in ["app/cli.py", "pyproject.toml"]:
            if not (staging / path).exists():
                raise RuntimeError(f"Release 缺少关键文件: {path}")

        # 原子 rename
        releases_dir.mkdir(parents=True, exist_ok=True)
        if release_dir.exists():
            shutil.rmtree(release_dir)
        os.replace(str(staging), str(release_dir))

        # 写入 current.json
        current_info = {
            "release_id": RELEASE_ID,
            "release_version": RELEASE_VERSION,
            "source_commit": manifest.get("source_commit", ""),
            "source_commit_short": SOURCE_COMMIT_SHORT,
            "builder_commit": manifest.get("builder_commit", ""),
            "payload_sha256": actual_hash,
            "installed_at": __import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        tmp = get_app_root() / "runtime" / "current.json.tmp"
        current_json = get_app_root() / "runtime" / "current.json"
        current_json.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(current_info, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(current_json))

    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    print(f"  Release 已安装: {release_dir}")
    return release_dir


def ensure_data_dirs(app_root: Path) -> None:
    """确保持久数据目录存在。

    :param app_root: 应用根目录。
    """
    for d in ["data", "storage", "models", "vendor", "bin", "logs"]:
        (app_root / d).mkdir(parents=True, exist_ok=True)


def ensure_env(app_root: Path, source_dir: Path) -> None:
    """如果 .env 不存在，从模板创建。

    :param app_root: 应用根目录。
    :param source_dir: 源码目录。
    """
    env_path = app_root / ".env"
    if env_path.exists():
        return

    template = source_dir / ".env.example"
    if template.exists():
        env_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        print("  .env 已从模板创建")


# ── 环境准备 ─────────────────────────────────────────────────────


def _find_system_python() -> Path | None:
    """查找系统 Python 3.11+。"""
    candidates = ["python", "python3", "py"]
    for name in candidates:
        try:
            result = subprocess.run(
                [name, "-c", "import sys; print(sys.version_info[:2])"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            v = tuple(int(x) for x in result.stdout.strip().strip("()").split(","))
            if v >= (3, 11):
                full = subprocess.run(
                    ["where", name] if sys.platform == "win32" else ["which", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                lines = full.stdout.strip().splitlines()
                for line in lines:
                    p = Path(line.strip())
                    if p.exists() and ".venv" not in str(p):
                        return p
        except Exception:
            continue
    return None


def _find_portable_python(app_root: Path) -> Path | None:
    """查找 portable-python 目录中的 Python。

    :param app_root: 应用根目录。
    :returns: Python 路径。
    """
    pp = app_root / "portable-python"
    if not pp.exists():
        return None
    if sys.platform == "win32":
        candidates = [
            pp / "python.exe",
            pp / "python3.exe",
        ]
    else:
        candidates = [
            pp / "bin" / "python3",
            pp / "bin" / "python",
        ]
    for c in candidates:
        if c.exists():
            return c
    return None


def prepare_venv(app_root: Path) -> Path:
    """准备虚拟环境。

    :param app_root: 应用根目录。
    :returns: venv python 路径。
    """
    venv_python = app_root / VENV_DIR / ("Scripts" if sys.platform == "win32" else "bin") / "python.exe" if sys.platform == "win32" else app_root / VENV_DIR / "bin" / "python"

    if sys.platform == "win32":
        venv_python = app_root / VENV_DIR / "Scripts" / "python.exe"
    else:
        venv_python = app_root / VENV_DIR / "bin" / "python"

    if venv_python.exists():
        return venv_python

    # 查找 Python
    system_py = _find_portable_python(app_root) or _find_system_python()
    if system_py is None:
        raise RuntimeError(
            "未找到 Python 3.11+。请安装 Python 或在 portable-python/ 目录放置 Portable Python。\n"
            "Portable Python 下载: https://www.python.org/downloads/windows/"
        )

    print(f"  Python: {system_py}")
    print("  创建虚拟环境...")
    subprocess.run(
        [str(system_py), "-m", "venv", str(app_root / VENV_DIR)],
        check=True,
        timeout=120,
    )
    return venv_python


def install_dependencies(venv_python: Path, app_root: Path, req_file: Path) -> None:
    """安装 Python 依赖。

    :param venv_python: venv python 路径。
    :param app_root: 应用根目录。
    :param req_file: requirements 文件。
    """
    # 检查是否已安装
    try:
        subprocess.run(
            [str(venv_python), "-c",
             "import fastapi, uvicorn, sqlmodel, pydantic; print('ok')"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        print("  依赖已安装")
        return
    except subprocess.CalledProcessError:
        pass

    wheels_dir = app_root / WHEELS_DIR
    if wheels_dir.exists() and list(wheels_dir.glob("*.whl")):
        # 离线安装
        print(f"  离线安装依赖 ({len(list(wheels_dir.glob('*.whl')))} wheels)...")
        subprocess.run(
            [
                str(venv_python), "-m", "pip", "install",
                "--no-index", "--find-links", str(wheels_dir),
                "-r", str(req_file),
            ],
            check=True,
            timeout=600,
        )
    else:
        # 联网安装 (国内镜像)
        print("  联网安装依赖 (国内镜像)...")
        subprocess.run(
            [
                str(venv_python), "-m", "pip", "install",
                "-r", str(req_file),
                "-i", PIP_INDEX,
                "--extra-index-url", PIP_EXTRA_INDEX,
                *[f"--trusted-host={h}" for h in PIP_TRUSTED_HOSTS],
            ],
            check=True,
            timeout=900,
        )
    print("  依赖安装完成")


# ── 启动 ────────────────────────────────────────────────────────


def _fail(msg: str) -> None:
    """输出错误并退出。

    :param msg: 错误消息。
    """
    print()
    print("*" * 60)
    for line in msg.strip().split("\n"):
        print(f"  [错误] {line}")
    print("*" * 60)
    print()
    print("按 Enter 键退出...")
    input()
    sys.exit(1)


def main() -> None:
    """Portable Launcher 主入口。"""
    app_root = get_app_root()
    os.chdir(str(app_root))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    print("=" * 60)
    print(f"  {APP_NAME} {VERSION} — Portable 启动器")
    print("=" * 60)
    print(f"  工作目录: {app_root}")
    print(f"  GitHub 请求: 0 (源码来自内置 Payload)")
    print()

    try:
        # 1. 确保持久数据目录
        ensure_data_dirs(app_root)

        # 2. 检查/安装 Runtime
        source_dir = get_current_release_dir()
        if source_dir is None:
            print("[1/5] 从内置 Payload 安装源码...")
            source_dir = install_source_from_payload(app_root)
            print()
        else:
            print(f"[1/5] Runtime 已就绪: {source_dir}")
            print()

        # 3. 确保 .env
        print("[2/5] 配置文件...")
        ensure_env(app_root, source_dir)
        print()

        # 4. 准备虚拟环境
        print("[3/5] Python 环境...")
        venv_python = prepare_venv(app_root)
        print()

        # 5. 安装依赖
        print("[4/5] 依赖安装...")
        req_file = source_dir / "requirements-bundle.txt"
        if not req_file.exists():
            # Fallback: try to find it
            req_file = app_root / "requirements-bundle.txt"
        if not req_file.exists():
            req_file = Path("requirements-bundle.txt")
        if req_file.exists():
            install_dependencies(venv_python, app_root, req_file)
        else:
            print(f"  [警告] 未找到 {req_file}，跳过依赖安装")
        print()

        # 6. 启动 Web
        print("[5/5] 启动 Web 控制台...")
        print()
        print("=" * 60)
        print("  浏览器将自动打开: http://127.0.0.1:8000")
        print("  按 Ctrl+C 停止服务")
        print("=" * 60)
        print()

        env = os.environ.copy()
        bin_dir = app_root / "bin"
        if (bin_dir / "ffmpeg.exe").exists():
            env["FFMPEG_PATH"] = str(bin_dir / "ffmpeg.exe")
            env["FFPROBE_PATH"] = str(bin_dir / "ffprobe.exe")
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

        # 注入源码路径
        env["BLC_PORTABLE"] = "1"
        env["BLC_SOURCE_DIR"] = str(source_dir)

        webbrowser.open("http://127.0.0.1:8000")

        subprocess.run(
            [
                str(venv_python), "-m", "app.cli", "serve",
                "--host", "127.0.0.1", "--port", "8000",
            ],
            env=env,
            cwd=str(app_root),
        )
    except KeyboardInterrupt:
        print("\n服务已停止")
    except Exception:
        print("\n服务异常退出:")
        traceback.print_exc()
        print("\n按 Enter 键退出...")
        input()


if __name__ == "__main__":
    main()
