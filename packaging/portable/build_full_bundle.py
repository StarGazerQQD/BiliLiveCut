"""Portable Full 离线包构建脚本。

输出: BiliLiveCut-Portable-Full-v0.1.14.5-alpha-x64.zip
内容: EXE + Portable Python + 离线 Wheels + FFmpeg + 模型
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PORTABLE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTABLE_DIR.parent.parent
DIST_DIR = PORTABLE_DIR / "dist" / "full"
LITE_DIR = PORTABLE_DIR / "dist" / "lite"
PAYLOAD_DIR = PORTABLE_DIR / "dist" / "payload"

RELEASE_VERSION = "0.1.14.5-alpha"
FULL_NAME = f"BiliLiveCut-Portable-Full-{RELEASE_VERSION}-x64"


def build_full_bundle() -> Path:
    """构建 Portable Full 离线包。

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

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bundle = tmp / FULL_NAME
        bundle.mkdir(parents=True)

        # 复制 EXE
        shutil.copy2(lite_path, bundle / "BiliLiveCut-Portable.exe")
        print("  [OK] 已复制 EXE")

        # README
        readme = bundle / "README.txt"
        readme.write_text(f"""\
BiliLiveCut Portable Full {RELEASE_VERSION}
============================================

完全离线启动包 — 无需网络、无需 Python、无需 FFmpeg。

文件结构:
  BiliLiveCut-Portable.exe    # 启动器 (双击运行)
  portable-python/            # 内嵌 Python 运行时
  vendor/wheels/              # 离线依赖包
  bin/                        # FFmpeg/FFprobe

首次启动:
  双击 BiliLiveCut-Portable.exe
  → 自动从内置 Payload 释放源码 (Commit: 74c21b4)
  → 自动检测 portable-python
  → 自动安装依赖 (--no-index, 使用本地 wheels)
  → 启动 Web 控制台

数据目录 (运行后生成):
  data/       数据库
  storage/    录制文件和成片
  models/     Whisper 模型
  logs/       日志
  .env        配置文件

来源:
  业务源码基线: 74c21b4
  发布版本: {RELEASE_VERSION}
  构建时间: {__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
""", encoding="utf-8")

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
        }
        (bundle / "checksums.json").write_text(
            json.dumps(checksums, ensure_ascii=False, indent=2), encoding="utf-8"
        )

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
        }

        (DIST_DIR / "build-manifest.json").write_text(
            json.dumps(build_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (DIST_DIR / "SHA256SUMS.txt").write_text(
            f"{zip_sha256}  {FULL_NAME}.zip\n", encoding="utf-8"
        )

        print(f"\n  [OK] {zip_path.name} ({zip_path.stat().st_size / 1024 / 1024:.1f} MB)")
        print(f"  SHA256: {zip_sha256[:32]}")

    return zip_path


if __name__ == "__main__":
    build_full_bundle()
