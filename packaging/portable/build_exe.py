"""Portable Lite 构建脚本 — 将 Launcher + Payload 编译为单个 EXE。

用法:
    python build_exe.py            # 先构建 Payload，再编译 EXE
    python build_exe.py --skip-payload  # 仅编译 (已有 Payload)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PORTABLE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTABLE_DIR.parent.parent
SPEC_FILE = PORTABLE_DIR / "portable_launcher.spec"
DIST_DIR = PORTABLE_DIR / "dist" / "lite"
PAYLOAD_DIR = PORTABLE_DIR / "dist" / "payload"
MANIFEST_PATH = PAYLOAD_DIR / "payload_manifest.json"

RELEASE_VERSION = "0.1.14.5-alpha"


def build_payload_if_needed() -> None:
    """确保 Payload 已构建。"""
    if not MANIFEST_PATH.exists() or not (PAYLOAD_DIR / "source_payload.zip").exists():
        print("[build_exe] Payload 不存在，开始构建...")
        sys.path.insert(0, str(PORTABLE_DIR))
        sys.path.insert(0, str(PROJECT_ROOT))
        from build_payload import build_payload

        build_payload()
        print("[build_exe] Payload 构建完成")


def build_exe() -> Path:
    """构建 Portable Lite EXE。

    :returns: 生成的 EXE 路径。
    """
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  BiliLiveCut Portable Lite {RELEASE_VERSION}")
    print("=" * 60)

    # 构建 Payload
    build_payload_if_needed()

    # 验证 Manifest
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    print(f"  Payload SHA256: {manifest['payload_sha256'][:32]}")
    print(f"  Source: {manifest['source_commit_short']}")

    # PyInstaller 构建
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(PORTABLE_DIR / "build" / "pyinstaller"),
        str(SPEC_FILE),
    ]

    print(f"\n  执行: {' '.join(cmd[:3])} ...")
    result = subprocess.run(cmd, cwd=str(PORTABLE_DIR))

    if result.returncode != 0:
        print(f"\n[错误] PyInstaller 编译失败 (退出码 {result.returncode})")
        sys.exit(1)

    exe_path = DIST_DIR / f"BiliLiveCut-Portable-Lite-{RELEASE_VERSION}-x64.exe"
    if not exe_path.exists():
        print(f"\n[错误] 未生成 {exe_path}")
        sys.exit(1)

    # 生成 build-manifest.json
    build_manifest = {
        "release_version": RELEASE_VERSION,
        "source_commit": manifest["source_commit"],
        "source_commit_short": manifest["source_commit_short"],
        "builder_commit": manifest["builder_commit"],
        "architecture": "x64",
        "artifact_type": "lite",
        "payload_sha256": manifest["payload_sha256"],
        "artifact_sha256": "",
    }

    import hashlib

    hasher = hashlib.sha256()
    with open(exe_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    build_manifest["artifact_sha256"] = hasher.hexdigest()

    bm_path = DIST_DIR / "build-manifest.json"
    bm_path.write_text(json.dumps(build_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # SHA256SUMS
    sums_path = DIST_DIR / "SHA256SUMS.txt"
    sums_path.write_text(f"{build_manifest['artifact_sha256']}  {exe_path.name}\n", encoding="utf-8")

    size_mb = exe_path.stat().st_size / (1024 * 1024)
    print(f"\n  [OK] {exe_path.name} ({size_mb:.1f} MB)")
    print(f"  SHA256: {build_manifest['artifact_sha256'][:32]}")

    return exe_path


if __name__ == "__main__":
    build_exe()
