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

PORTABLE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTABLE_DIR.parent.parent
DIST_DIR = PORTABLE_DIR / "dist" / "full"
LITE_DIR = PORTABLE_DIR / "dist" / "lite"
PAYLOAD_DIR = PORTABLE_DIR / "dist" / "payload"
RESOURCES_DIR = PORTABLE_DIR / "resources"

RELEASE_VERSION = "0.1.14.6-alpha"
FULL_NAME = f"BiliLiveCut-Portable-Full-{RELEASE_VERSION}-x64"


def build_full_bundle() -> Path:
    """构建 Portable Full 包。

    注意: Full 不包含四引擎模型。模型由独立 Engine Pack 提供。

    :returns: ZIP 文件路径。
    """
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  BiliLiveCut Portable Full {RELEASE_VERSION}")
    print("=" * 60)

    # 确保 Lite EXE 存在
    lite_exe_name = f"BiliLiveCut-Portable-Lite-{RELEASE_VERSION}-x64.exe"
    lite_path = LITE_DIR / lite_exe_name
    if not lite_path.exists():
        print("[错误] 请先构建 Lite EXE: python build_exe.py")
        sys.exit(1)
    print(f"  EXE: {lite_path.stat().st_size / 1024 / 1024:.1f} MB")

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


if __name__ == "__main__":
    build_full_bundle()
