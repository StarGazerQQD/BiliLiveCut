"""Portable Lite 构建脚本 — 将 Launcher + Payload + Engine Pack Info 编译为单个 EXE。

用法:
    python build_exe.py            # 先构建 Payload，再编译 EXE
    python build_exe.py --skip-payload  # 仅编译 (已有 Payload)

CI 环境:
    设置 BLC_CI_BUILD=1 可跳过 Engine Pack 校验 (不嵌入 engine_pack_info.json)。
    CI 构建的 EXE 不含 Engine Pack 元数据，最终发布时需本地重建。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PORTABLE_DIR = Path(__file__).resolve().parent.parent.parent.parent
PROJECT_ROOT = PORTABLE_DIR.parent.parent
SPEC_FILE = PORTABLE_DIR / "specs" / "portable_launcher.spec"
DIST_DIR = PORTABLE_DIR / "dist" / "lite"
PAYLOAD_DIR = PORTABLE_DIR / "dist" / "payload"
MANIFEST_PATH = PAYLOAD_DIR / "payload_manifest.json"
RESOURCES_DIR = PORTABLE_DIR / "resources"
ENGINE_PACK_INFO_PATH = RESOURCES_DIR / "engine_pack_info.json"

RELEASE_VERSION = "0.1.14.7-alpha"


def build_payload_if_needed() -> None:
    """确保 Payload 已构建。"""
    if not MANIFEST_PATH.exists() or not (PAYLOAD_DIR / "source_payload.zip").exists():
        print("[build_exe] Payload missing, building ...")
        from blc_portable.payload.builder import build_payload

        build_payload()
        print("[build_exe] Payload built successfully")


def check_engine_pack_info() -> None:
    """验证 Engine Pack 信息文件存在且 CRC32/SHA-256 非空。

    设置环境变量 BLC_CI_BUILD=1 可跳过所有校验 (CI 环境无法构建真正的 Engine Pack)。

    :raises RuntimeError: Engine Pack 信息不完整 (非 CI 模式)。
    """
    is_ci = os.environ.get("BLC_CI_BUILD") == "1"

    # CI 模式: 完全跳过，无论文件是否存在
    if is_ci:
        print("  [CI] BLC_CI_BUILD=1, skip Engine Pack info validation")
        return

    if not ENGINE_PACK_INFO_PATH.exists():
        raise RuntimeError(
            "engine_pack_info.json missing: " + str(ENGINE_PACK_INFO_PATH) + "\n"
            "Run: python build_engine_pack.py --from-cache"
        )

    ep_info = json.loads(ENGINE_PACK_INFO_PATH.read_text(encoding="utf-8"))
    crc32 = str(ep_info.get("crc32", ""))
    sha256 = str(ep_info.get("sha256", ""))
    version = str(ep_info.get("engine_pack_version", ""))
    filename = str(ep_info.get("filename", ""))

    errors: list[str] = []

    if not crc32:
        errors.append("CRC32 is empty -- build Engine Pack first")
    elif len(crc32) != 8 or not all(c in "0123456789ABCDEF" for c in crc32):
        errors.append(f"CRC32 invalid: {crc32} (expect 8 uppercase hex chars)")

    if not sha256:
        errors.append("SHA-256 is empty -- build Engine Pack first")
    elif len(sha256) != 64:
        errors.append(f"SHA-256 invalid: {sha256} (expect 64 hex chars)")

    if version != RELEASE_VERSION:
        errors.append(f"Engine Pack version mismatch: {version} != {RELEASE_VERSION}")

    if not filename:
        errors.append("filename is empty")

    expected_ids = ep_info.get("expected_engine_ids", [])
    if not expected_ids:
        errors.append("expected_engine_ids is empty")

    if errors:
        error_msg = "Engine Pack validation FAILED:\n  - " + "\n  - ".join(errors)
        raise RuntimeError(error_msg)

    print(f"  Engine Pack OK: CRC32={crc32} SHA256={sha256[:16]}...")


def _generate_default_engine_pack_info() -> dict:
    """生成默认 Engine Pack 信息占位（仅供 Engine Pack 构建本身使用）。

    注意: 此函数的目的是辅助 Engine Pack 构建流程生成初始占位文件。
    Lite/Final 构建不应调用此函数，必须有真实的 Engine Pack 信息。

    :returns: 默认占位字典。
    """
    return {
        "format_version": 2,
        "engine_pack_version": RELEASE_VERSION,
        "compatible_app": {"min": RELEASE_VERSION, "max_exclusive": "0.1.15"},
        "filename": f"BiliLiveCut-EnginePack-{RELEASE_VERSION}.zip",
        "size_bytes": 0,
        "crc32": "",
        "sha256": "",
        "manifest_sha256": "",
        "source_commit": "",
        "builder_commit": "",
        "expected_engine_ids": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
    }


def build_exe() -> Path:
    """Build Portable Lite EXE.

    :returns: Path to generated EXE.
    """
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  BiliLiveCut Portable Lite {RELEASE_VERSION}")
    print("=" * 60)

    # Build Payload
    build_payload_if_needed()

    # Validate Engine Pack info
    check_engine_pack_info()

    # Validate Manifest
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    print(f"  Payload SHA256: {manifest['payload_sha256'][:32]}")
    print(f"  Source: {manifest['source_commit_short']}")

    # Read Engine Pack info
    if ENGINE_PACK_INFO_PATH.exists():
        ep_info = json.loads(ENGINE_PACK_INFO_PATH.read_text(encoding="utf-8"))
        print(f"  Engine Pack: {ep_info.get('filename', 'N/A')} CRC32={ep_info.get('crc32', 'N/A')}")

    # PyInstaller build
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

    print("\n  PyInstaller compiling ...")
    result = subprocess.run(cmd, cwd=str(PORTABLE_DIR))

    if result.returncode != 0:
        print(f"\n[Error] PyInstaller failed (exit code {result.returncode})")
        sys.exit(1)

    exe_path = DIST_DIR / f"BiliLiveCut-Portable-Lite-v{RELEASE_VERSION}-x64.exe"
    if not exe_path.exists():
        print(f"\n[Error] EXE not found: {exe_path}")
        sys.exit(1)

    # 生成 build-manifest.json
    is_ci = os.environ.get("BLC_CI_BUILD") == "1"
    build_manifest = {
        "release_version": RELEASE_VERSION,
        "source_commit": manifest["source_commit"],
        "source_commit_short": manifest["source_commit_short"],
        "builder_commit": manifest["builder_commit"],
        "architecture": "x64",
        "artifact_type": "lite",
        "payload_sha256": manifest["payload_sha256"],
        "artifact_sha256": "",
        "ci_build": is_ci,
    }

    # 如果 Engine Pack 信息存在，添加 CRC32
    if ENGINE_PACK_INFO_PATH.exists():
        ep_info = json.loads(ENGINE_PACK_INFO_PATH.read_text(encoding="utf-8"))
        build_manifest["engine_pack_crc32"] = ep_info.get("crc32", "")

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
    if "engine_pack_crc32" in build_manifest:
        print(f"  Embedded Engine Pack CRC32: {build_manifest['engine_pack_crc32']}")

    return exe_path


def main() -> int:
    """入口 — 供薄入口调用。

    :returns: 0 成功, 1 失败。
    """
    try:
        build_exe()
        return 0
    except SystemExit as e:
        return int(str(e)) if str(e) else 0
    except Exception as exc:
        # Windows GBK/cp1252 encoding may fail on Chinese error messages
        try:
            print(f"[Error] {exc}")
        except UnicodeEncodeError:
            print(f"[Error] {exc!r}")
        return 1


if __name__ == "__main__":
    build_exe()
