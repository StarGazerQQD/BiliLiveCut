"""Portable Full 完整包构建脚本。

输出: BiliLiveCut-Portable-Full-v0.1.14.6-alpha-x64.zip
内容: EXE + Portable Python + 离线 Wheels + FFmpeg (不含模型)

注意: 模型不由 Full 包携带。四个 ASR 引擎模型统一由独立的 Engine Pack 提供。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

PORTABLE_DIR = Path(__file__).resolve().parent.parent.parent.parent
PROJECT_ROOT = PORTABLE_DIR.parent.parent
DIST_DIR = PORTABLE_DIR / "dist" / "full"
LITE_DIR = PORTABLE_DIR / "dist" / "lite"
PAYLOAD_DIR = PORTABLE_DIR / "dist" / "payload"
RESOURCES_DIR = PORTABLE_DIR / "resources"

RELEASE_VERSION = "0.1.14.7-alpha"
FULL_NAME = f"BiliLiveCut-Portable-Full-{RELEASE_VERSION}-x64"


def build_full_bundle() -> Path:
    """构建 Portable Full 离线包。

    Full 包必须实际包含:
    - BiliLiveCut-Portable.exe (Lite EXE)
    - portable-python/ (内嵌 Python 运行时)
    - vendor/wheels/ (离线依赖包)
    - bin/ffmpeg.exe, bin/ffprobe.exe
    - README.txt, checksums.json, SHA256SUMS.txt

    注意: Full 不包含四引擎模型。模型由独立 Engine Pack 提供。

    :returns: ZIP 文件路径。
    :raises RuntimeError: 关键组件缺失时。
    """
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  BiliLiveCut Portable Full {RELEASE_VERSION}")
    print("=" * 60)

    # 确保 Lite EXE 存在
    lite_exe_name = f"BiliLiveCut-Portable-Lite-v{RELEASE_VERSION}-x64.exe"
    lite_path = LITE_DIR / lite_exe_name
    if not lite_path.exists():
        raise RuntimeError(f"Lite EXE 不存在: {lite_path}\n请先构建: python build_exe.py")
    print(f"  EXE: {lite_path.stat().st_size / 1024 / 1024:.1f} MB")

    # 验证必须的离线组件
    missing: list[str] = []

    portable_py = app_root / "portable-python" / "python.exe" if PORTABLE_DIR else None
    if not portable_py:
        portable_py = PROJECT_ROOT / "portable-python" / "python.exe"
    if not portable_py.exists():
        missing.append(f"portable-python/python.exe (需预先准备 Portable Python)")

    wheels_dir = app_root / "vendor" / "wheels"
    if not wheels_dir.exists() or not list(wheels_dir.glob("*.whl")):
        candidate = PROJECT_ROOT / "vendor" / "wheels"
        if candidate.exists() and list(candidate.glob("*.whl")):
            wheels_dir = candidate
        else:
            missing.append("vendor/wheels/ (无 .whl 文件 — 离线安装将不可用)")

    ffmpeg = app_root / "bin" / "ffmpeg.exe"
    if not ffmpeg.exists():
        candidate = PROJECT_ROOT / "bin" / "ffmpeg.exe"
        if candidate.exists():
            ffmpeg = candidate
        else:
            missing.append("bin/ffmpeg.exe")

    ffprobe = app_root / "bin" / "ffprobe.exe"
    if not ffprobe.exists():
        candidate = PROJECT_ROOT / "bin" / "ffprobe.exe"
        if candidate.exists():
            ffprobe = candidate
        else:
            missing.append("bin/ffprobe.exe")

    if missing:
        print("\n  [警告] Full 离线包缺少以下组件:")
        for m in missing:
            print(f"    - {m}")
        print("  离线安装可能失败。将仅打包已存在的组件。")
        print("  Full 离线包构建前提:")
        print("    1. portable-python/ 目录 (Python 3.11/3.12)")
        print("    2. vendor/wheels/ 目录 (离线 Wheels)")
        print("    3. bin/ffmpeg.exe 和 bin/ffprobe.exe")
        print()

    # 读取 Engine Pack 信息 (如有)
    engine_pack_crc32 = ""
    ep_info_path = RESOURCES_DIR / "engine_pack_info.json"
    if ep_info_path.exists():
        ep_info = json.loads(ep_info_path.read_text(encoding="utf-8"))
        engine_pack_crc32 = ep_info.get("crc32", "")
        print(f"  Engine Pack CRC32: {engine_pack_crc32 or '(空)'}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bundle = tmp / FULL_NAME
        bundle.mkdir(parents=True)

        # 复制 EXE
        shutil.copy2(lite_path, bundle / "BiliLiveCut-Portable.exe")
        print("  [OK] 已复制 EXE")

        # 复制 portable-python (如果存在)
        pp_src = None
        for candidate in [
            PROJECT_ROOT / "portable-python",
            Path("portable-python"),
        ]:
            if candidate.exists() and (candidate / "python.exe").exists():
                pp_src = candidate
                break
        if pp_src:
            pp_dst = bundle / "portable-python"
            shutil.copytree(pp_src, pp_dst, dirs_exist_ok=True)
            py_count = sum(1 for _ in pp_dst.rglob("*.exe") if _.is_file())
            print(f"  [OK] 已复制 portable-python ({py_count} 可执行文件)")
        else:
            print("  [跳过] portable-python 不存在")

        # 复制 vendor/wheels (如果存在)
        wh_src = None
        for candidate in [
            PROJECT_ROOT / "vendor" / "wheels",
            Path("vendor") / "wheels",
        ]:
            if candidate.exists() and list(candidate.glob("*.whl")):
                wh_src = candidate
                break
        if wh_src:
            wh_dst = bundle / "vendor" / "wheels"
            wh_dst.mkdir(parents=True, exist_ok=True)
            wh_count = 0
            for whl in wh_src.glob("*.whl"):
                shutil.copy2(whl, wh_dst / whl.name)
                wh_count += 1
            print(f"  [OK] 已复制 {wh_count} 个 wheels")
        else:
            print("  [跳过] vendor/wheels 不存在")

        # 复制 FFmpeg (如果存在)
        for tool_name in ("ffmpeg.exe", "ffprobe.exe"):
            tool_src = None
            for candidate in [
                PROJECT_ROOT / "bin" / tool_name,
                Path("bin") / tool_name,
            ]:
                if candidate.exists():
                    tool_src = candidate
                    break
            if tool_src:
                tool_dst = bundle / "bin"
                tool_dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(tool_src, tool_dst / tool_name)
                print(f"  [OK] 已复制 {tool_name}")
            else:
                print(f"  [跳过] {tool_name} 不存在")

        # README
        import datetime

        readme = bundle / "README.txt"
        readme.write_text(
            f"""\
BiliLiveCut Portable Full {RELEASE_VERSION}
============================================

完整启动包 — 安装期间无需额外下载。

与 Lite 版的区别:
  - 内置 Portable Python (无需系统安装 Python)
  - 内置离线 Wheels (无需联网安装依赖)
  - 内置 FFmpeg/FFprobe (无需系统安装 FFmpeg)

不含模型:
  - 四个 ASR 引擎模型由独立 Engine Pack 提供。
  - 将 BiliLiveCut-EnginePack-{RELEASE_VERSION}.zip 放在同一目录，
    首次启动时自动校验 CRC32 并安装。
  - 如果没有 Engine Pack，首次启动时自动在线下载全部四个引擎模型。

文件结构:
  BiliLiveCut-Portable.exe    # 启动器 (双击运行)
  portable-python/            # 内嵌 Python 运行时
  vendor/wheels/              # 离线依赖包
  bin/                        # FFmpeg/FFprobe

首次启动:
  双击 BiliLiveCut-Portable.exe
  → 从内置 Payload 释放源码 (固定 Commit: 731a31c)
  → 检测 portable-python
  → 安装依赖 (--no-index, 使用本地 wheels)
  → 检测 Engine Pack 或在线下载模型
  → 启动 Web 控制台

数据目录 (运行后生成):
  data/       数据库
  storage/    录制文件和成片
  models/     四个 ASR 引擎模型 (由 Engine Pack 或在线下载安装)
  logs/       日志
  .env        配置文件

来源:
  业务源码基线: 731a31c
  发布版本: {RELEASE_VERSION}
  构建时间: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
""",
            encoding="utf-8",
        )

        # checksums.json
        manifest_path = PAYLOAD_DIR / "payload_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        import hashlib

        exe_sha256 = hashlib.sha256()
        with open(lite_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                exe_sha256.update(chunk)

        checksums = {
            "release_version": RELEASE_VERSION,
            "source_commit": manifest["source_commit"],
            "exe_sha256": exe_sha256.hexdigest(),
            "engine_pack_crc32": engine_pack_crc32,
        }
        (bundle / "checksums.json").write_text(json.dumps(checksums, ensure_ascii=False, indent=2), encoding="utf-8")

        # 打包 ZIP
        zip_path = DIST_DIR / f"{FULL_NAME}.zip"
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(bundle.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(tmp).as_posix()
                    zf.write(p, rel)

        # 生成 build-manifest.json 和 SHA256SUMS
        zip_sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        build_manifest = {
            "release_version": RELEASE_VERSION,
            "source_commit": manifest["source_commit"],
            "source_commit_short": manifest["source_commit_short"],
            "builder_commit": manifest["builder_commit"],
            "architecture": "x64",
            "artifact_type": "full",
            "payload_sha256": manifest["payload_sha256"],
            "artifact_sha256": zip_sha256,
            "engine_pack_crc32": engine_pack_crc32,
        }

        (DIST_DIR / "build-manifest.json").write_text(
            json.dumps(build_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (DIST_DIR / "SHA256SUMS.txt").write_text(f"{zip_sha256}  {FULL_NAME}.zip\n", encoding="utf-8")

        print(f"\n  [OK] {zip_path.name} ({zip_path.stat().st_size / 1024 / 1024:.1f} MB)")
        print(f"  SHA256: {zip_sha256[:32]}")

    return zip_path


def main() -> int:
    """入口 — 供薄入口调用。

    :returns: 0 成功, 1 失败。
    """
    try:
        build_full_bundle()
        return 0
    except SystemExit as e:
        return int(str(e)) if str(e) else 0
    except Exception as exc:
        print(f"[错误] {exc}")
        return 1


if __name__ == "__main__":
    build_full_bundle()
