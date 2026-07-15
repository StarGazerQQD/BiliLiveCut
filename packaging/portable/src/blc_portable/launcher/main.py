"""BiliLiveCut Portable Launcher — 从 EXE 内置 Payload 释放源码并启动。

运行流程:
1. 检查持久 Runtime → 如已安装且完好，直接启动
2. 读取 EXE 内置 Payload (source_payload.zip + payload_manifest.json)
3. 校验 Payload SHA-256
4. 原子安装到 runtime/releases/<release-id>/
5. Python/依赖/FFmpeg 检测
6. 模型准备: 已安装 → Engine Pack → 在线下载
7. 启动 app.cli serve
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

# ── 常量 ──────────────────────────────────────────────────
APP_NAME = "BiliLiveCut"
VERSION = "V0.1.14.8 Alpha"
RELEASE_VERSION = "0.1.14.8-alpha"
SOURCE_COMMIT_SHORT = "731a31c"
# 注意: RELEASE_ID 将在获得 Payload SHA-256 后动态生成 (内容寻址)

VENV_DIR = ".venv"
WHEELS_DIR = os.path.join("vendor", "wheels")

# 国内镜像
PIP_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
PIP_EXTRA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_TRUSTED_HOSTS = ["mirrors.aliyun.com", "pypi.tuna.tsinghua.edu.cn"]

# FFmpeg
FFMPEG_WIN_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"


# ── 资源路径 ──────────────────────────────────────────────


def get_bundled_resource_path(rel: str) -> Path | None:
    """获取打包资源路径，兼容 PyInstaller 和普通运行。

    :param rel: 相对路径。
    :returns: 资源路径，不存在返回 None。
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent.parent.parent.parent / "dist" / "payload"

    p = base / rel
    return p if p.exists() else None


def get_payload_zip() -> Path:
    """获取内嵌 Payload ZIP 路径。

    :returns: Payload ZIP 路径。
    :raises RuntimeError: 找不到时。
    """
    p = get_bundled_resource_path("source_payload.zip")
    if p is None:
        raise RuntimeError("找不到内置 Payload (source_payload.zip)。请确保 EXE 已正确嵌入 Payload。")
    return p


def get_payload_manifest() -> dict[str, Any]:
    """读取内嵌 Manifest。

    :returns: Manifest 字典。
    :raises RuntimeError: 找不到时。
    """
    p = get_bundled_resource_path("payload_manifest.json")
    if p is None:
        raise RuntimeError("找不到内置 Manifest (payload_manifest.json)。")
    return json.loads(p.read_text(encoding="utf-8"))


def get_engine_pack_info() -> dict[str, Any] | None:
    """读取内置 Engine Pack 信息。

    :returns: Engine Pack 信息字典，未嵌入返回 None。
    """
    p = get_bundled_resource_path("engine_pack_info.json")
    if p is None:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ── Runtime 管理 ──────────────────────────────────────────


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

    使用内容寻址: {version}+{commit}+{payload_hash_prefix}

    :returns: Release 目录，不存在返回 None。
    """
    current_path = get_app_root() / "runtime" / "current.json"
    if not current_path.exists():
        return None
    try:
        info = json.loads(current_path.read_text(encoding="utf-8"))
        rid = info.get("release_id", "")
        if not rid:
            return None
        # 检查 payload hash 匹配
        expected_payload_sha = info.get("payload_sha256", "")
        d = get_releases_dir() / rid
        if d.exists() and (d / "app" / "cli.py").exists():
            # 进一步校验: 比较 EXE 内嵌 Payload SHA 与安装时记录的 SHA
            try:
                manifest = get_payload_manifest()
                exe_payload_sha = manifest.get("payload_sha256", "")
                if exe_payload_sha and expected_payload_sha:
                    if exe_payload_sha != expected_payload_sha:
                        print(
                            f"  Payload 已变更"
                            f" (EXE SHA={exe_payload_sha[:16]}"
                            f" != installed SHA={expected_payload_sha[:16]})，需要重新安装"
                        )
                        return None
            except RuntimeError:
                pass  # Manifest 不可读，继续使用现有 Runtime
            return d
        return None
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
        raise RuntimeError(f"Payload 哈希不匹配: actual={actual_hash[:16]} expected={expected_hash[:16]}")

    if manifest.get("release_version") != RELEASE_VERSION:
        raise RuntimeError(f"Payload 版本不匹配: {manifest.get('release_version')} != {RELEASE_VERSION}")
    if manifest.get("source_commit_short") != SOURCE_COMMIT_SHORT:
        raise RuntimeError(f"Source Commit 不匹配: {manifest.get('source_commit_short')} != {SOURCE_COMMIT_SHORT}")

    print(f"  Payload: v{RELEASE_VERSION} | Source: {SOURCE_COMMIT_SHORT} | SHA256: {actual_hash[:16]}")

    # 内容寻址 Release ID: version + source commit + payload hash prefix
    payload_hash_short = actual_hash[:12]
    content_release_id = f"{RELEASE_VERSION}+{SOURCE_COMMIT_SHORT}+{payload_hash_short}"

    releases_dir = get_releases_dir()
    staging = get_app_root() / "runtime" / "staging"
    release_dir = releases_dir / content_release_id

    if staging.exists():
        shutil.rmtree(staging)

    try:
        staging.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                # Zip Slip 防御
                if member.startswith("/") or ".." in member.split("/") or ":" in member:
                    raise RuntimeError(f"ZIP 包含不安全路径: {member}")

                target = (staging / member).resolve()
                if not str(target).startswith(str(staging.resolve())):
                    raise RuntimeError(f"ZIP 路径越界: {member}")

                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # 流式解压 — 避免大文件整读入内存
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst, length=8 * 1024 * 1024)

        for path in ["app/cli.py", "pyproject.toml"]:
            if not (staging / path).exists():
                raise RuntimeError(f"Release 缺少关键文件: {path}")

        releases_dir.mkdir(parents=True, exist_ok=True)
        if release_dir.exists():
            shutil.rmtree(release_dir)
        os.replace(str(staging), str(release_dir))

        current_info = {
            "runtime_schema": 2,
            "release_id": content_release_id,
            "release_version": RELEASE_VERSION,
            "source_commit": manifest.get("source_commit", ""),
            "source_commit_short": SOURCE_COMMIT_SHORT,
            "builder_commit": manifest.get("builder_commit", ""),
            "payload_sha256": actual_hash,
            "manifest_sha256": manifest.get("payload_sha256", ""),
            "python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
            "platform": sys.platform,
            "architecture": "x64" if sys.maxsize > 2**32 else "x86",
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


# ── 环境准备 ──────────────────────────────────────────────


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
        candidates = [pp / "python.exe", pp / "python3.exe"]
    else:
        candidates = [pp / "bin" / "python3", pp / "bin" / "python"]
    for c in candidates:
        if c.exists():
            return c
    return None


def prepare_venv(app_root: Path) -> Path:
    """准备虚拟环境。

    :param app_root: 应用根目录。
    :returns: venv python 路径。
    """
    if sys.platform == "win32":
        venv_python = app_root / VENV_DIR / "Scripts" / "python.exe"
    else:
        venv_python = app_root / VENV_DIR / "bin" / "python"

    if venv_python.exists():
        return venv_python

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
    try:
        subprocess.run(
            [str(venv_python), "-c", "import fastapi, uvicorn, sqlmodel, pydantic; print('ok')"],
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
        print(f"  安装依赖 (本地 {len(list(wheels_dir.glob('*.whl')))} wheels)...")
        subprocess.run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheels_dir),
                "-r",
                str(req_file),
            ],
            check=True,
            timeout=600,
        )
    else:
        print("  安装依赖 (国内镜像)...")
        subprocess.run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "-r",
                str(req_file),
                "-i",
                PIP_INDEX,
                "--extra-index-url",
                PIP_EXTRA_INDEX,
                *[f"--trusted-host={h}" for h in PIP_TRUSTED_HOSTS],
            ],
            check=True,
            timeout=900,
        )
    print("  依赖安装完成")


# ── 模型准备 ──────────────────────────────────────────────


def prepare_models(app_root: Path, user_engine_pack_path: str | None = None) -> dict[str, Any]:
    """模型准备编排 — 已安装 → Engine Pack → 在线下载。

    1. 检查已安装模型 → 版本匹配直接复用
    2. 查找本地 Engine Pack → CRC32 校验通过则从包安装 (网络 0)
    3. 本地包无效 → 全量在线下载四引擎 (网络 N)
    4. 无本地包 → 全量在线下载四引擎

    无论如何不混合本地包与在线模型。

    :param app_root: 应用根目录。
    :param user_engine_pack_path: 用户通过 --engine-pack 指定的路径。
    :returns: 模型准备信息字典。
    """
    from ..engine_pack.installer import check_installed_models, find_local_engine_pack, install_from_engine_pack

    MODEL_ENGINE_PACK_VERSION = "0.1.14.8-alpha"

    # 读取内置 Engine Pack 信息
    pack_info = get_engine_pack_info()
    if pack_info is None:
        pack_info = {
            "engine_pack_version": MODEL_ENGINE_PACK_VERSION,
            "filename": f"BiliLiveCut-EnginePack-{MODEL_ENGINE_PACK_VERSION}.zip",
            "crc32": "",
            "expected_engine_ids": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
        }

    expected_filename = str(pack_info.get("filename", ""))
    expected_crc32 = str(pack_info.get("crc32", ""))
    expected_sha256 = str(pack_info.get("sha256", ""))
    expected_version = str(pack_info.get("engine_pack_version", MODEL_ENGINE_PACK_VERSION))

    # 1. 检查已安装模型
    models_dir = app_root / "models"
    if check_installed_models(models_dir, expected_version):
        print("  四引擎模型已安装 (版本匹配)，跳过模型准备")
        return {
            "source": "already_installed",
            "network_requests": 0,
        }

    # 2. 查找本地 Engine Pack
    pack_path = find_local_engine_pack(app_root, expected_filename, user_engine_pack_path)

    if pack_path is not None:
        print(f"\n  找到本地 Engine Pack: {pack_path.name}")
        try:
            return install_from_engine_pack(app_root, pack_path, expected_crc32, expected_sha256, expected_version)
        except RuntimeError:
            # CRC32 不匹配或解压/校验失败 → 不使用本地包
            print("  本地包未通过校验，切换在线下载...")
    else:
        print("\n  未找到本地 Engine Pack")

    # 3. 全量在线下载
    print("  全量在线下载四引擎模型...")
    try:
        from .model_downloader import download_all_engines

        return download_all_engines(app_root)
    except ImportError as exc:
        print(f"  [警告] 模型下载依赖缺失: {exc}")
        print("  将继续启动 (ASR 可能使用系统已缓存的模型)")
        return {
            "source": "skipped",
            "network_requests": 0,
            "warning": str(exc),
        }


# ── 启动 ──────────────────────────────────────────────────


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


def _run_doctor(app_root: Path) -> None:
    """运行系统诊断检查。

    检查项:
    - Runtime 是否已安装
    - models/ 是否完整
    - Python/venv 可用性
    - 关键文件存在性
    - 校验信息完整性

    :param app_root: 应用根目录。
    """
    import platform

    print("=" * 60)
    print(f"  {APP_NAME} Doctor — 系统诊断")
    print("=" * 60)
    print()

    checks_passed = 0
    checks_total = 0

    def _check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal checks_passed, checks_total
        checks_total += 1
        status = "[OK]" if condition else "[FAIL]"
        msg = f"  {status} {name}"
        if detail:
            msg += f": {detail}"
        print(msg)
        if condition:
            checks_passed += 1

    # 1. Runtime
    current = get_current_release_dir()
    _check("Runtime 已安装", current is not None, str(current) if current else "未安装")

    # 2. Payload
    try:
        manifest = get_payload_manifest()
        _check("Payload Manifest 可读", True, f"v{manifest.get('release_version')}")
    except RuntimeError:
        _check("Payload Manifest 可读", False, "不可读")

    # 3. Engine Pack 信息
    ep = get_engine_pack_info()
    _check("Engine Pack 信息嵌入", ep is not None)
    if ep:
        _check("Engine Pack CRC32 非空", bool(ep.get("crc32")), str(ep.get("crc32"))[:12])
        _check(
            "Engine Pack SHA-256 非空", bool(ep.get("sha256")), str(ep.get("sha256"))[:12] if ep.get("sha256") else "空"
        )

    # 4. Python 可用
    py = _find_system_python()
    _check("系统 Python 3.11+", py is not None, str(py) if py else "未找到")
    pp = _find_portable_python(app_root)
    _check("Portable Python", pp is not None, str(pp) if pp else "未找到")

    # 5. Models
    models_dir = app_root / "models"
    if models_dir.exists():
        for engine_id in ("whisper", "paraformer", "sensevoice", "funasr_nano"):
            epath = models_dir / engine_id
            _check(
                f"引擎 {engine_id}",
                epath.exists() and any(epath.iterdir()),
                f"{sum(1 for _ in epath.rglob('*') if _.is_file()) if epath.exists() else 0} 文件",
            )
    else:
        _check("models 目录", False, "不存在")

    # 6. FFmpeg
    ffmpeg = app_root / "bin" / "ffmpeg.exe"
    _check("FFmpeg", ffmpeg.exists(), str(ffmpeg) if ffmpeg.exists() else "未找到")

    # 7. 平台信息
    print()
    print(f"  Python: {platform.python_version()}")
    print(f"  Platform: {platform.platform()}")
    print(f"  Architecture: {'x64' if sys.maxsize > 2**32 else 'x86'}")

    print()
    print(f"  诊断完成: {checks_passed}/{checks_total} 项通过")


def _verify_installed_models(app_root: Path) -> None:
    """验证已安装模型完整性（逐文件 SHA-256 重算）。

    :param app_root: 应用根目录。
    """
    import hashlib

    from ..engine_pack.installer import _read_installed_manifest

    models_dir = app_root / "models"
    installed = _read_installed_manifest(models_dir)

    if installed is None:
        print("  [FAIL] 未找到已安装模型清单")
        sys.exit(1)

    print("=" * 60)
    print("  模型完整性验证")
    print("=" * 60)
    print(f"  安装版本: {installed.get('engine_pack_version')}")
    print(f"  安装时间: {installed.get('installed_at')}")
    print(f"  引擎: {installed.get('engine_ids', [])}")
    print()

    verified = 0
    failed = 0
    for engine_id, info in installed.get("files", {}).items():
        target_path = str(info.get("target_path", f"models/{engine_id}"))
        engine_dir = models_dir / engine_id if (models_dir / engine_id).exists() else models_dir / target_path
        if not engine_dir.exists():
            print(f"  [FAIL] 引擎 {engine_id}: 目录不存在")
            failed += 1
            continue

        fc = 0
        ts = 0
        for f in engine_dir.rglob("*"):
            if f.is_file():
                hashlib.sha256(f.read_bytes()).hexdigest()
                fc += 1
                ts += f.stat().st_size
        print(f"  [OK] 引擎 {engine_id}: {fc} 文件, {ts / (1024**3):.2f} GB (SHA-256 已重算)")
        verified += 1

    if failed:
        print(f"\n  [FAIL] {failed} 个引擎存在问题")
        sys.exit(1)
    else:
        print(f"\n  [OK] 全部 {verified} 个引擎验证通过")
    """Portable Launcher 主入口 — 支持 argparse 参数解析。"""
    import argparse

    parser = argparse.ArgumentParser(
        description=f"BiliLiveCut {VERSION} — Portable 启动器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  直接启动:                    BiliLiveCut-Portable.exe
  指定 Engine Pack:            BiliLiveCut-Portable.exe --engine-pack ./BiliLiveCut-EnginePack.zip
  离线模式:                    BiliLiveCut-Portable.exe --offline
  验证已安装模型:               BiliLiveCut-Portable.exe --verify-models
  运行故障诊断:                 BiliLiveCut-Portable.exe --doctor
  修复损坏 Runtime:             BiliLiveCut-Portable.exe --repair
  查看版本:                    BiliLiveCut-Portable.exe --version
""",
    )
    parser.add_argument("--engine-pack", type=str, default=None, metavar="PATH", help="指定本地 Engine Pack ZIP 路径")
    parser.add_argument("--offline", action="store_true", help="离线模式: 禁止联网下载模型 (仅使用本地 Engine Pack)")
    parser.add_argument(
        "--fallback-online", action="store_true", help="Engine Pack 校验失败时允许联网下载 (仅 --engine-pack 模式)"
    )
    parser.add_argument("--verify-runtime", action="store_true", help="验证已安装 Runtime 完整性")
    parser.add_argument("--verify-models", action="store_true", help="验证已安装模型完整性 (逐文件 SHA-256)")
    parser.add_argument("--repair", action="store_true", help="修复模式: 重新安装 Runtime 和模型")
    parser.add_argument("--doctor", action="store_true", help="运行系统诊断检查")
    parser.add_argument("--version", action="store_true", help="显示版本信息并退出")

    args = parser.parse_args()

    # --version
    if args.version:
        print(f"BiliLiveCut Portable {VERSION}")
        print(f"Release Version: {RELEASE_VERSION}")
        print(f"Source Commit: {SOURCE_COMMIT_SHORT}")
        manifest = get_payload_manifest()
        print(f"Payload SHA256: {manifest.get('payload_sha256', 'N/A')[:32]}")
        pack_info = get_engine_pack_info()
        if pack_info:
            print(f"Engine Pack: {pack_info.get('engine_pack_version', 'N/A')} CRC32={pack_info.get('crc32', 'N/A')}")
        sys.exit(0)

    user_engine_pack_path = args.engine_pack

    app_root = get_app_root()
    os.chdir(str(app_root))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    print("=" * 60)
    print(f"  {APP_NAME} {VERSION} — Portable 启动器")
    print("=" * 60)
    print(f"  工作目录: {app_root}")
    print("  GitHub 请求: 0 (源码来自内置 Payload)")
    print()

    try:
        # --doctor 模式
        if args.doctor:
            _run_doctor(app_root)
            sys.exit(0)

        # --verify-models 模式
        if args.verify_models:
            _verify_installed_models(app_root)
            sys.exit(0)

        # --repair 模式
        if args.repair:
            current = get_app_root() / "runtime" / "current.json"
            if current.exists():
                current.unlink()
            releases = get_releases_dir()
            if releases.exists():
                shutil.rmtree(releases)
            print("[修复] 已清理旧 Runtime，将重新安装")
            print()

        # 1. 确保持久数据目录
        ensure_data_dirs(app_root)

        # 2. 检查/安装 Runtime
        source_dir = get_current_release_dir()
        if source_dir is None:
            print("[1/6] 从内置 Payload 安装源码...")
            source_dir = install_source_from_payload(app_root)
            print()
        else:
            print(f"[1/6] Runtime 已就绪: {source_dir}")
            print()

        # 3. 确保 .env
        print("[2/6] 配置文件...")
        ensure_env(app_root, source_dir)
        print()

        # 4. 准备虚拟环境
        print("[3/6] Python 环境...")
        venv_python = prepare_venv(app_root)
        print()

        # 5. 安装依赖
        print("[4/6] 依赖安装...")
        req_file = source_dir / "requirements-bundle.txt"
        if not req_file.exists():
            req_file = app_root / "requirements-bundle.txt"
        if not req_file.exists():
            req_file = Path("requirements-bundle.txt")
        if req_file.exists():
            install_dependencies(venv_python, app_root, req_file)
        else:
            print(f"  [警告] 未找到 {req_file}，跳过依赖安装")
        print()

        # 6. 模型准备
        print("[5/6] 模型准备...")
        model_result = prepare_models(app_root, user_engine_pack_path)
        model_source = model_result.get("source", "unknown")
        network_reqs = model_result.get("network_requests", 0)
        print(f"  模型来源: {model_source} (网络请求: {network_reqs})")
        print()

        # 7. 启动 Web
        print("[6/6] 启动 Web 控制台...")
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

        env["BLC_PORTABLE"] = "1"
        env["BLC_SOURCE_DIR"] = str(source_dir)

        # 设置模型目录环境变量 (供 FunASR/Whisper 使用)
        models_dir = app_root / "models"
        if models_dir.exists():
            env["BLC_MODELS_DIR"] = str(models_dir)

        webbrowser.open("http://127.0.0.1:8000")

        subprocess.run(
            [
                str(venv_python),
                "-m",
                "app.cli",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
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
    main()  # noqa: F821 (由 PyInstaller .spec 文件在顶层定义)
