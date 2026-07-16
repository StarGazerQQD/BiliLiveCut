"""BiliLiveCut Portable Launcher — 从 EXE 内置 Payload 释放源码并启动。

Startup flow:
1. Check persistent Runtime → if installed and intact, launch directly
2. Read EXE built-in Payload (source_payload.zip + payload_manifest.json)
3. Verify Payload SHA-256
4. Atomic install to runtime/releases/<release-id>/
5. Python/deps/FFmpeg detection
6. 模型准备: installed -> Engine Pack -> online download
7. Launch app.cli serve
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# -- Constants ──────────────────────────────────────────────────
APP_NAME = "BiliLiveCut"
VERSION = "V0.1.14.10 Alpha"
RELEASE_VERSION = "0.1.14.10-alpha"
SOURCE_COMMIT_SHORT = "731a31c"
# NOTE: RELEASE_ID 将在获得 Payload SHA-256 后动态生成 (内容寻址)

VENV_DIR = ".venv"
WHEELS_DIR = os.path.join("vendor", "wheels")

# China mirror
PIP_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
PIP_EXTRA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_TRUSTED_HOSTS = ["mirrors.aliyun.com", "pypi.tuna.tsinghua.edu.cn"]

# FFmpeg
FFMPEG_WIN_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"


# -- Resource paths ──────────────────────────────────────────────


def get_bundled_resource_path(rel: str) -> Path | None:
    """Get bundled resource path, compatible with PyInstaller and normal run。

    :param rel: relative path。
    :returns: 资源路径，not found返回 None。
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent.parent.parent.parent / "dist" / "payload"

    p = base / rel
    return p if p.exists() else None


def get_payload_zip() -> Path:
    """Get embedded Payload ZIP path。

    :returns: Payload ZIP 路径。
    :raises RuntimeError: 找不到时。
    """
    p = get_bundled_resource_path("source_payload.zip")
    if p is None:
        raise RuntimeError("Built-in Payload not found (source_payload.zip). Ensure EXE embeds Payload correctly.")
    return p


def get_payload_manifest() -> dict[str, Any]:
    """Read embedded Manifest。

    :returns: Manifest dict。
    :raises RuntimeError: 找不到时。
    """
    p = get_bundled_resource_path("payload_manifest.json")
    if p is None:
        raise RuntimeError("Built-in Manifest not found (payload_manifest.json).")
    return json.loads(p.read_text(encoding="utf-8"))


def get_engine_pack_info() -> dict[str, Any] | None:
    """Read embedded Engine Pack info。

    :returns: Engine Pack info dict, None if not embedded。
    """
    p = get_bundled_resource_path("engine_pack_info.json")
    if p is None:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# -- Runtime management ──────────────────────────────────────────


def get_app_root() -> Path:
    """Get Portable app root dir。委托给 runtime 模块。"""
    from blc_portable.runtime import get_app_root as _get

    return _get()


def get_releases_dir() -> Path:
    """Get releases dir。委托给 runtime 模块。"""
    from blc_portable.runtime import get_releases_dir as _get

    return _get()


def get_current_release_dir() -> Path | None:
    """Get currently active Release dir。委托给 runtime 模块。"""
    from blc_portable.runtime import get_current_release_dir as _get

    return _get()


def install_source_from_payload(app_root: Path) -> Path:
    """Atomic install from embedded Payload to Runtime。

    :param app_root: app root dir。
    :returns: installed Release dir。
    """
    import hashlib

    manifest = get_payload_manifest()
    zip_path = get_payload_zip()

    # Verify Payload SHA-256
    hasher = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    actual_hash = hasher.hexdigest()
    expected_hash = manifest.get("payload_sha256", "")
    if actual_hash != expected_hash:
        raise RuntimeError(f"Payload hash mismatch: actual={actual_hash[:16]} expected={expected_hash[:16]}")

    if manifest.get("release_version") != RELEASE_VERSION:
        raise RuntimeError(f"Payload version mismatch: {manifest.get('release_version')} != {RELEASE_VERSION}")
    if manifest.get("source_commit_short") != SOURCE_COMMIT_SHORT:
        raise RuntimeError(f"Source Commit mismatch: {manifest.get('source_commit_short')} != {SOURCE_COMMIT_SHORT}")

    print(f"  Payload: v{RELEASE_VERSION} | Source: {SOURCE_COMMIT_SHORT} | SHA256: {actual_hash[:16]}")

    # 内容寻址 Release ID: version + source commit + payload hash prefix
    payload_hash_short = actual_hash[:12]
    content_release_id = f"{RELEASE_VERSION}+{SOURCE_COMMIT_SHORT}+{payload_hash_short}"

    releases_dir = get_releases_dir()
    staging = get_app_root() / "runtime" / "staging"
    release_dir = releases_dir / content_release_id

    if staging.exists():
        shutil.rmtree(staging)

    from blc_portable.archive.locks import FileLock, get_runtime_lock_path

    lock = FileLock(get_runtime_lock_path(app_root))

    with lock.acquire(timeout=120):
        try:
            staging.mkdir(parents=True, exist_ok=True)

            from blc_portable.archive.safe_zip import safe_extract

            with zipfile.ZipFile(zip_path) as zf:
                safe_extract(zf, staging)

            for path in ["app/cli.py", "pyproject.toml"]:
                if not (staging / path).exists():
                    raise RuntimeError(f"Release missing key file: {path}")

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

    print(f"  Release installed: {release_dir}")
    return release_dir


def ensure_data_dirs(app_root: Path) -> None:
    """ensure persistent data dirs存在。

    :param app_root: app root dir。
    """
    for d in ["data", "storage", "models", "vendor", "bin", "logs"]:
        (app_root / d).mkdir(parents=True, exist_ok=True)


def ensure_env(app_root: Path, source_dir: Path) -> None:
    """如果 .env not found，从模板创建。

    :param app_root: app root dir。
    :param source_dir: source dir。
    """
    env_path = app_root / ".env"
    if env_path.exists():
        return

    template = source_dir / ".env.example"
    if template.exists():
        env_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        print("  .env created from template")


# -- Environment prep ──────────────────────────────────────────────


def _find_system_python() -> Path | None:
    """查找System Python 3.11+。"""
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
    """Find Python in portable-python dir。

    :param app_root: app root dir。
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
    """Prepare virtual environment。

    :param app_root: app root dir。
    :returns: venv python path。
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
            "Python 3.11+ not found. Install Python or place Portable Python in portable-python/.\n"
            "Portable Python download: https://www.python.org/downloads/windows/"
        )

    # Validate Python version (only 3.11 and 3.12 supported)
    r = subprocess.run(
        [str(system_py), "-c", "import sys; v=sys.version_info[:2]; print(f'{v[0]}.{v[1]}')"],
        capture_output=True, text=True, timeout=10,
    )
    py_ver = r.stdout.strip()
    parts = py_ver.split(".")
    major, minor = int(parts[0]), int(parts[1])
    if major > 3 or (major == 3 and minor >= 13):
        raise RuntimeError(
            f"Python {py_ver} is not supported. Only Python 3.11 and 3.12.\n"
            "Download: https://www.python.org/downloads/windows/"
        )

    print(f"  Python: {system_py} ({py_ver})")

    print(f"  Python: {system_py}")
    print("  creating venv...")
    subprocess.run(
        [str(system_py), "-m", "venv", str(app_root / VENV_DIR)],
        check=True,
        timeout=120,
    )
    return venv_python


def install_dependencies(venv_python: Path, app_root: Path, req_file: Path) -> None:
    """Install Python dependencies。

    :param venv_python: venv python path。
    :param app_root: app root dir。
    :param req_file: requirements files。
    """
    try:
        subprocess.run(
            [str(venv_python), "-c", "import fastapi, uvicorn, sqlmodel, pydantic; print('ok')"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        print("  dependencies already installed")
        return
    except subprocess.CalledProcessError:
        pass

    # Select ABI-specific lock file
    r = subprocess.run(
        [str(venv_python), "-c", "import sys; v=sys.version_info[:2]; print(f'py{v[0]}{v[1]}')"],
        capture_output=True, text=True, timeout=10,
    )
    abi = r.stdout.strip()
    lock_dir = Path(__file__).resolve().parent.parent.parent.parent / "packaging" / "portable" / "locks"
    lock_file = lock_dir / f"requirements-runtime-{abi}-win-x64.lock"

    if not lock_file.exists():
        raise RuntimeError(f"Lock file not found: {lock_file.name}\nOnly Python 3.11 and 3.12 are supported.")

    # Install from lock file
    print(f"  install deps (lock file: {lock_file.name})...")
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-r", str(lock_file)],
        check=True, timeout=600,
    )

    # Import smoke check
    print("  import smoke check...")
    for mod in ("fastapi", "uvicorn", "sqlmodel", "pydantic", "app.cli"):
        subprocess.run(
            [str(venv_python), "-c", f"import {mod}; print('  ok: {mod}')"],
            check=True, capture_output=True, timeout=30,
        )
    print("  deps install complete")

    wheels_dir = app_root / WHEELS_DIR
    if wheels_dir.exists() and list(wheels_dir.glob("*.whl")):
        print(f"  install deps (local {len(list(wheels_dir.glob('*.whl')))} wheels)...")
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
        print("  install deps (mirror)...")
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
    print("  deps install complete")


# -- Model preparation ──────────────────────────────────────────────


def prepare_models(app_root: Path, user_engine_pack_path: str | None = None) -> dict[str, Any]:
    """Model prep orchestration — installed -> Engine Pack -> online download。

    1. check installed models -> version match = reuse
    2. find local Engine Pack -> CRC32 passes = install from pack (zero network)
    3. local pack invalid -> full online download (N requests)
    4. no local pack -> full online download

    Never mix local pack with online models。

    :param app_root: app root dir。
    :param user_engine_pack_path: 用户通过 --engine-pack 指定的路径。
    :returns: 模型准备信息字典。
    """
    from ..engine_pack.installer import check_installed_models, find_local_engine_pack, install_from_engine_pack

    MODEL_ENGINE_PACK_VERSION = "0.1.14.10-alpha"

    # Read embedded Engine Pack info
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
        print("  4-engine models installed (version match), skip model prep")
        return {
            "source": "already_installed",
            "network_requests": 0,
        }

    # 2. 查找本地 Engine Pack
    pack_path = find_local_engine_pack(app_root, expected_filename, user_engine_pack_path)

    if pack_path is not None:
        print(f"\n  found local Engine Pack: {pack_path.name}")
        try:
            return install_from_engine_pack(app_root, pack_path, expected_crc32, expected_sha256, expected_version)
        except RuntimeError:
            # CRC32 不匹配或解压/校验失败 → 不使用本地包
            print("  local pack failed validation, falling back to online download")
    else:
        print("\n  no local Engine Pack found")

    # 3. Online download (blocked if --offline)
    if os.environ.get("BLC_OFFLINE") == "1" or os.environ.get("PIP_NO_INDEX") == "1":
        raise RuntimeError(
            "Offline mode: no local Engine Pack found and online download is blocked.\n"
            "Provide an Engine Pack ZIP or remove --offline flag."
        )

    print("  downloading all 4 engine models online...")
    try:
        from .model_downloader import download_all_engines

        return download_all_engines(app_root)
    except ImportError as exc:
        print(f"  [WARN] model download dependency missing: {exc}")
        print("  continue startup (ASR may use cached models)")
        return {
            "source": "skipped",
            "network_requests": 0,
            "warning": str(exc),
        }


# -- Launch ──────────────────────────────────────────────────


def _fail(msg: str) -> None:
    """Print error and exit。

    :param msg: 错误消息。
    """
    print()
    print("*" * 60)
    for line in msg.strip().split("\n"):
        print(f"  [Error] {line}")
    print("*" * 60)
    print()
    print("Press Enter to exit...")
    input()
    sys.exit(1)


def _run_doctor(app_root: Path) -> None:
    """Run System Diagnostics check。

    检查项:
    - Runtime 是否已安装
    - models/ 是否完整
    - Python/venv 可用性
    - 关键文件存在性
    - 校验信息完整性

    :param app_root: app root dir。
    """
    import platform

    print("=" * 60)
    print(f"  {APP_NAME} Doctor — System Diagnostics")
    print("=" * 60)
    print()

    checks_passed = 0
    checks_warned = 0
    checks_failed = 0

    def _check(
        name: str, condition: bool, detail: str = "", expected: str = "", actual: str = "", suggestion: str = ""
    ) -> None:
        nonlocal checks_passed, checks_warned, checks_failed
        if condition:
            status = "[PASS]"
            checks_passed += 1
        else:
            status = "[FAIL]"
            checks_failed += 1
        msg = f"  {status} {name}"
        if detail:
            msg += f": {detail}"
        print(msg)
        if not condition and expected:
            print(f"         expected: {expected}")
        if not condition and actual:
            print(f"         actual: {actual}")
        if not condition and suggestion:
            print(f"         suggestion: {suggestion}")

    def _warn(name: str, detail: str = "") -> None:
        nonlocal checks_warned
        checks_warned += 1
        print(f"  [WARN] {name}: {detail}")

    # 1. Runtime
    current = get_current_release_dir()
    _check("Runtime installed", current is not None, str(current) if current else "not installed")

    # 2. Payload
    try:
        manifest = get_payload_manifest()
        _check("Payload Manifest readable", True, f"v{manifest.get('release_version')}")
    except RuntimeError:
        _check("Payload Manifest readable", False, "not readable")

    # 3. Engine Pack 信息
    ep = get_engine_pack_info()
    _check("Engine Pack info embedded", ep is not None)
    if ep:
        _check("Engine Pack CRC32 non-empty", bool(ep.get("crc32")), str(ep.get("crc32"))[:12])
        _check(
            "Engine Pack SHA-256 non-empty",
            bool(ep.get("sha256")),
            str(ep.get("sha256"))[:12] if ep.get("sha256") else "empty",
        )

    # 4. Python 可用
    py = _find_system_python()
    _check("System Python 3.11+", py is not None, str(py) if py else "not found")
    pp = _find_portable_python(app_root)
    _check("Portable Python", pp is not None, str(pp) if pp else "not found")

    # 5. Models
    models_dir = app_root / "models"
    if models_dir.exists():
        for engine_id in ("whisper", "paraformer", "sensevoice", "funasr_nano"):
            epath = models_dir / engine_id
            _check(
                f"engine {engine_id}",
                epath.exists() and any(epath.iterdir()),
                f"{sum(1 for _ in epath.rglob('*') if _.is_file()) if epath.exists() else 0} files",
            )
    else:
        _check("models dir", False, "not found")

    # 6. FFmpeg
    ffmpeg = app_root / "bin" / "ffmpeg.exe"
    _check("FFmpeg", ffmpeg.exists(), str(ffmpeg) if ffmpeg.exists() else "not found")

    # 7. 平台信息
    print()
    print(f"  Python: {platform.python_version()}")
    print(f"  Platform: {platform.platform()}")
    print(f"  Architecture: {'x64' if sys.maxsize > 2**32 else 'x86'}")

    print()
    print(f"  Diagnostics complete: {checks_passed} PASS, {checks_warned} WARN, {checks_failed} FAIL")


def _verify_installed_models(app_root: Path) -> None:
    """验证已安装模型完整性（per-file SHA-256 recompute）。

    :param app_root: app root dir。
    """
    import hashlib

    from ..engine_pack.installer import _read_installed_manifest

    models_dir = app_root / "models"
    installed = _read_installed_manifest(models_dir)

    if installed is None:
        print("  [FAIL] installed model manifest not found")
        sys.exit(1)

    print("=" * 60)
    print("  Model Integrity Verification")
    print("=" * 60)
    print(f"  Installed version: {installed.get('engine_pack_version')}")
    print(f"  installed at: {installed.get('installed_at')}")
    print(f"  engines: {installed.get('engine_ids', [])}")
    print()

    verified = 0
    failed = 0
    total_files_checked = 0
    hash_mismatches = 0

    # 逐引擎比对安装清单中记录的文件
    for engine_id, info in installed.get("files", {}).items():
        target_path = str(info.get("target_path", f"models/{engine_id}"))
        engine_dir = models_dir / engine_id if (models_dir / engine_id).exists() else models_dir / target_path
        if not engine_dir.exists():
            print(f"  [FAIL] engine {engine_id}: dirnot found")
            failed += 1
            continue

        engine_file_count = info.get("file_count", 0)
        engine_total_size = info.get("total_size", 0)

        fc = 0
        ts = 0
        for f in engine_dir.rglob("*"):
            if f.is_file():
                expected_hash = ""
                # Check if installed manifest has per-file info
                rel = f.relative_to(models_dir).as_posix()
                file_info = installed.get("file_details", {}).get(rel, {})
                expected_hash = str(file_info.get("sha256", ""))
                actual_hash = hashlib.sha256(f.read_bytes()).hexdigest()
                if expected_hash and actual_hash != expected_hash:
                    hash_mismatches += 1
                fc += 1
                ts += f.stat().st_size
                total_files_checked += 1

        # Verify against manifest
        if engine_file_count and fc != engine_file_count:
            print(f"  [WARN] engine {engine_id}: file count mismatch declared={engine_file_count} actual={fc}")
        if engine_total_size and ts != engine_total_size:
            print(f"  [WARN] engine {engine_id}: size mismatch declared={engine_total_size} actual={ts}")

        print(f"  [PASS] engine {engine_id}: {fc} files, {ts / (1024**3):.2f} GB (SHA-256 recomputed)")
        verified += 1

    if hash_mismatches:
        print(f"  [FAIL] {hash_mismatches}  file(s) SHA-256 mismatch")
        failed += 1

    if failed:
        print(f"\n  [FAIL] {failed}  engines have issues")
        sys.exit(1)
    else:
        print(f"\n  [PASS] All {verified} engines verified OK ({total_files_checked} files)")


def _repair_runtime(app_root: Path) -> None:
    """Clear old Runtime to trigger reinstall。"""
    from blc_portable.runtime.activation import delete_current_json

    delete_current_json(app_root)
    releases = app_root / "runtime" / "releases"
    if releases.exists():
        shutil.rmtree(releases)
    print("[Repair] cleared old Runtime, will reinstall")


def build_parser() -> argparse.ArgumentParser:
    """Build command-line argument parser。

    :returns: ArgumentParser 实例。
    """
    import argparse

    parser = argparse.ArgumentParser(
        description=f"BiliLiveCut {VERSION} — Portable Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    Launch:                  BiliLiveCut-Portable.exe
  Specify Engine Pack:      BiliLiveCut-Portable.exe --engine-pack ./BiliLiveCut-EnginePack.zip
    Offline:                 BiliLiveCut-Portable.exe --offline
    Verify models:           BiliLiveCut-Portable.exe --verify-models
    Doctor:                  BiliLiveCut-Portable.exe --doctor
  Repair damaged Runtime:             BiliLiveCut-Portable.exe --repair
    Version:                 BiliLiveCut-Portable.exe --version
""",
    )
    parser.add_argument(
        "--engine-pack", type=str, default=None, metavar="PATH", help="Specify local Engine Pack ZIP path"
    )
    parser.add_argument(
        "--offline", action="store_true", help="Offline mode: block network model download (local Engine Pack only)"
    )
    parser.add_argument(
        "--fallback-online",
        action="store_true",
        help="Allow online download when Engine Pack validation fails (only with --engine-pack mode)",
    )
    parser.add_argument("--verify-runtime", action="store_true", help="Verify installed Runtime integrity")
    parser.add_argument(
        "--verify-models", action="store_true", help="Verify installed model integrity (per-file SHA-256)"
    )
    parser.add_argument("--repair", action="store_true", help="Repair mode: reinstall Runtime and models")
    parser.add_argument("--doctor", action="store_true", help="Run System Diagnostics check")
    parser.add_argument("--version", action="store_true", help="Show version info and exit")
    return parser


def run_launcher(args: argparse.Namespace) -> int:
    """Execute launch flow。

    :param args: 解析后的命令行参数。
    :returns: 退出码 (0 成功, 非零失败)。
    """
    user_engine_pack_path = args.engine_pack

    app_root = get_app_root()
    os.chdir(str(app_root))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    print("=" * 60)
    print(f"  {APP_NAME} {VERSION} — Portable Launcher")
    print("=" * 60)
    print(f"  Working dir: {app_root}")
    print("  GitHub requests: 0 (source from built-in Payload)")
    print()

    try:
        # --doctor 模式
        if args.doctor:
            _run_doctor(app_root)
            return 0

        # --verify-runtime 模式
        if args.verify_runtime:
            from blc_portable.runtime.verifier import verify_runtime

            ok, errors = verify_runtime(app_root)
            if errors:
                for e in errors:
                    print(f"  [FAIL] {e}")
            if ok:
                print("\n  [PASS] Runtime verification: PASS")
            else:
                print("\n  [FAIL] Runtime verification: FAIL")
            return 0 if ok else 1

        # --verify-models 模式
        if args.verify_models:
            _verify_installed_models(app_root)
            return 0

        # --offline 模式: blocking network requests
        if args.offline:
            os.environ["BLC_OFFLINE"] = "1"
            os.environ["PIP_NO_INDEX"] = "1"
            print("  [OFFLINE] Offline mode enabled -- all network requests blocked")

        # --repair 模式
        if args.repair:
            _repair_runtime(app_root)
            print()

        # 1. ensure persistent data dirs
        ensure_data_dirs(app_root)

        # 2. check/install Runtime
        source_dir = get_current_release_dir()
        if source_dir is None:
            print("[1/6] Installing source from built-in Payload...")
            source_dir = install_source_from_payload(app_root)
            print()
        else:
            print(f"[1/6] Runtime ready: {source_dir}")
            print()

        # 3. 确保 .env
        print("[2/6] Config file...")
        ensure_env(app_root, source_dir)
        print()

        # 4. Prepare virtual environment
        print("[3/6] Python environment...")
        venv_python = prepare_venv(app_root)
        print()

        # 5. 安装依赖
        print("[4/6] Dependency install...")
        req_file = source_dir / "requirements-bundle.txt"
        if not req_file.exists():
            req_file = app_root / "requirements-bundle.txt"
        if not req_file.exists():
            req_file = Path("requirements-bundle.txt")
        if req_file.exists():
            install_dependencies(venv_python, app_root, req_file)
        else:
            print(f"  [WARN] not found: {req_file}, skipping dependency install")
        print()

        # 6. 模型准备
        print("[5/6] Model preparation...")
        model_result = prepare_models(app_root, user_engine_pack_path)
        model_source = model_result.get("source", "unknown")
        network_reqs = model_result.get("network_requests", 0)
        print(f"  Model source: {model_source} (network requests: {network_reqs})")
        print()

        # 7. 启动 Web
        print("[6/6] Starting Web console...")
        print()
        print("=" * 60)
        print("  Browser will open: http://127.0.0.1:8000")
        print("  Press Ctrl+C to stop")
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
        env["PYTHONPATH"] = str(source_dir)

        models_dir = app_root / "models"
        if models_dir.exists():
            env["BLC_MODELS_DIR"] = str(models_dir)

        result = subprocess.run(
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
        return result.returncode

    except KeyboardInterrupt:
        print("\nService stopped")
        return 0
    except Exception:
        print("\nService exited with error:")
        traceback.print_exc()
        if sys.stdin.isatty():
            print("\nPress Enter to exit...")
            input()
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    """主入口 — importable and testable callable entrypoint。

    :param argv: 命令行参数列表 (None 使用 sys.argv)。
    :returns: 退出码 (0 成功, 非零失败)。
    """
    parser = build_parser()

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse 内置 help/error 处理already printed message
        return int(str(e)) if str(e) else 0

    # --version separate handling (不进入 run_launcher 的heavy startup flow)
    if args.version:
        print(f"BiliLiveCut Portable {VERSION}")
        print(f"Release Version: {RELEASE_VERSION}")
        print(f"Source Commit: {SOURCE_COMMIT_SHORT}")
        try:
            manifest = get_payload_manifest()
            print(f"Payload SHA256: {manifest.get('payload_sha256', 'N/A')[:32]}")
        except RuntimeError:
            print("Payload SHA256: (unreadable)")
        pack_info = get_engine_pack_info()
        if pack_info:
            print(f"Engine Pack: {pack_info.get('engine_pack_version', 'N/A')} CRC32={pack_info.get('crc32', 'N/A')}")
        return 0

    return run_launcher(args)


if __name__ == "__main__":
    raise SystemExit(main())
