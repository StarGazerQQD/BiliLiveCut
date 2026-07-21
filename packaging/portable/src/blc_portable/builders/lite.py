"""Portable Lite 构建脚本 — 将 Launcher + Payload + Engine Pack Info 编译为单个 EXE。

用法:
    python build_exe.py            # 先构建 Payload，再编译 EXE
    python build_exe.py --skip-payload  # 仅编译 (已有 Payload)

CI 环境:
    设置 BLC_FIXTURE_BUILD=1 可跳过 Engine Pack 校验 (仅 PR/CI 快速测试)。
    正式 Release 禁止使用任何 bypass 环境变量，必须嵌入完整 Engine Pack 元数据。
"""

from __future__ import annotations

import argparse
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

RELEASE_VERSION = "0.1.14.11-alpha"


def build_payload_if_needed() -> None:
    """确保 Payload 已构建。"""
    if not MANIFEST_PATH.exists() or not (PAYLOAD_DIR / "source_payload.zip").exists():
        print("[build_exe] Payload missing, building ...")
        from blc_portable.payload.builder import build_payload

        build_payload()
        print("[build_exe] Payload built successfully")


def check_engine_pack_info() -> None:
    """验证 Engine Pack 信息文件存在且 CRC32/SHA-256 非空。

    校验链:
    - CRC32 (8 hex) / SHA-256 (64 hex) / size_bytes / filename
    - engine_pack_version 匹配
    - artifact_class 必须显式存在 — 缺失或为 fixture → 失败
    - format_version >= 4
    - content_manifest_sha256 / model_lock_sha256 非空且 64 hex
    - expected_engine_ids 为 [whisper, paraformer, sensevoice, funasr_nano]
    - engine_pack_api_version / model_set_version 存在
    - size_bytes >= 500 MB (排除 fixture)

    设置环境变量 BLC_FIXTURE_BUILD=1 可跳过所有校验 (仅 PR/CI 快速测试用途)。
    正式 Release 禁止使用任何 bypass 环境变量。

    :raises RuntimeError: Engine Pack 信息不完整 (非 Fixture 模式)。
    """
    import json
    import os

    is_fixture = os.environ.get("BLC_FIXTURE_BUILD") == "1"

    if is_fixture:
        print("  [FIXTURE] BLC_FIXTURE_BUILD=1, skip Engine Pack info validation")
        return

    ENGINE_PACK_INFO_PATH = PORTABLE_DIR / "resources" / "engine_pack_info.json"

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
    size_bytes = int(ep_info.get("size_bytes", 0))
    content_manifest_sha = str(ep_info.get("content_manifest_sha256", ""))
    model_lock_sha = str(ep_info.get("model_lock_sha256", ""))
    expected_ids = list(ep_info.get("expected_engine_ids", []))
    artifact_class = str(ep_info.get("artifact_class", ""))
    format_version = int(ep_info.get("format_version", 0))
    engine_pack_api_ver = int(ep_info.get("engine_pack_api_version", 0))
    model_set_ver = int(ep_info.get("model_set_version", 0))

    errors: list[str] = []

    # 1. CRC32
    if not crc32:
        errors.append("CRC32 is empty — build Engine Pack first")
    elif len(crc32) != 8 or not all(c in "0123456789ABCDEF" for c in crc32):
        errors.append(f"CRC32 invalid: {crc32} (expect 8 uppercase hex chars)")

    # 2. SHA-256
    if not sha256:
        errors.append("SHA-256 is empty — build Engine Pack first")
    elif len(sha256) != 64:
        errors.append(f"SHA-256 invalid length: {len(sha256)} (expect 64 hex chars)")

    # 3. Version
    if version != RELEASE_VERSION:
        errors.append(f"Engine Pack version mismatch: {version} != {RELEASE_VERSION}")

    # 4. Filename
    if not filename:
        errors.append("filename is empty")

    # 5. artifact_class — 必须显式存在
    if not artifact_class:
        errors.append("artifact_class is missing — must be explicitly 'production' or 'fixture'")
    elif artifact_class != "production":
        errors.append(f"artifact_class is {artifact_class!r} — production build requires 'production'")

    # 6. format_version
    if format_version < 4:
        errors.append(f"format_version must be >= 4, got {format_version}")

    # 7. Content manifest SHA-256
    if not content_manifest_sha:
        errors.append("content_manifest_sha256 is empty")
    elif len(content_manifest_sha) != 64:
        errors.append(f"content_manifest_sha256 invalid length: {len(content_manifest_sha)} (expect 64)")

    # 8. Model lock SHA-256
    if not model_lock_sha:
        errors.append("model_lock_sha256 is empty")
    elif len(model_lock_sha) != 64:
        errors.append(f"model_lock_sha256 invalid length: {len(model_lock_sha)} (expect 64)")

    # 9. Expected engine IDs
    required_ids = {"whisper", "paraformer", "sensevoice", "funasr_nano"}
    if not expected_ids:
        errors.append("expected_engine_ids is empty")
    else:
        actual_ids = set(expected_ids)
        if actual_ids != required_ids:
            errors.append(f"expected_engine_ids mismatch: got {sorted(actual_ids)} need {sorted(required_ids)}")

    # 10. Engine Pack API version
    if engine_pack_api_ver < 1:
        errors.append(f"engine_pack_api_version invalid: {engine_pack_api_ver}")
    if model_set_ver < 1:
        errors.append(f"model_set_version invalid: {model_set_ver}")

    # 11. Minimum production size
    if size_bytes < 500_000_000:
        errors.append(f"size_bytes too small for production ({size_bytes} < 500 MB) — likely a fixture")

    if errors:
        error_msg = "Engine Pack validation FAILED:\n  - " + "\n  - ".join(errors)
        raise RuntimeError(error_msg)

    print(
        f"  Engine Pack OK: CRC32={crc32} SHA256={sha256[:16]}... "
        f"API={engine_pack_api_ver} Models={model_set_ver} Size={size_bytes / (1024**3):.1f} GB"
    )


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


def build_exe(*, without_engine_pack: bool = False) -> Path:
    """Build Portable Lite EXE.

    :param without_engine_pack: Build without embedded Engine Pack metadata.
    :returns: Path to generated EXE.
    """
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  BiliLiveCut Portable Lite {RELEASE_VERSION}")
    print("=" * 60)

    # Build Payload
    build_payload_if_needed()

    # Official GitHub releases do not distribute the multi-gigabyte Engine Pack.
    if without_engine_pack:
        print("  Engine Pack metadata: omitted (user-supplied pack or online model download)")
    else:
        check_engine_pack_info()

    # Validate Manifest
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    print(f"  Payload SHA256: {manifest['payload_sha256'][:32]}")
    print(f"  Source: {manifest['source_commit_short']}")

    # Read Engine Pack info
    if not without_engine_pack and ENGINE_PACK_INFO_PATH.exists():
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
    build_env = os.environ.copy()
    if without_engine_pack:
        build_env["BLC_OMIT_ENGINE_PACK_INFO"] = "1"
    else:
        build_env.pop("BLC_OMIT_ENGINE_PACK_INFO", None)
    result = subprocess.run(cmd, cwd=str(PORTABLE_DIR), env=build_env)

    if result.returncode != 0:
        print(f"\n[Error] PyInstaller failed (exit code {result.returncode})")
        sys.exit(1)

    exe_path = DIST_DIR / f"BiliLiveCut-Portable-Lite-v{RELEASE_VERSION}-x64.exe"
    if not exe_path.exists():
        print(f"\n[Error] EXE not found: {exe_path}")
        sys.exit(1)

    # 生成 build-manifest.json
    is_fixture = os.environ.get("BLC_FIXTURE_BUILD") == "1"
    build_manifest = {
        "release_version": RELEASE_VERSION,
        "source_commit": manifest["source_commit"],
        "source_commit_short": manifest["source_commit_short"],
        "builder_commit": manifest["builder_commit"],
        "architecture": "x64",
        "artifact_type": "lite",
        "payload_sha256": manifest["payload_sha256"],
        "artifact_sha256": "",
        "ci_build": is_fixture,
        "engine_pack_metadata": "omitted" if without_engine_pack else "embedded",
    }

    # 如果 Engine Pack 信息存在，添加 CRC32
    if not without_engine_pack and ENGINE_PACK_INFO_PATH.exists():
        ep_info = json.loads(ENGINE_PACK_INFO_PATH.read_text(encoding="utf-8"))
        build_manifest["engine_pack_crc32"] = ep_info.get("crc32", "")

    import hashlib
    import zlib

    hasher = hashlib.sha256()
    crc_val: int = 0
    with open(exe_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
            crc_val = zlib.crc32(chunk, crc_val)
    build_manifest["artifact_sha256"] = hasher.hexdigest()
    build_manifest["artifact_crc32"] = f"{crc_val & 0xFFFFFFFF:08X}"

    bm_path = DIST_DIR / "build-manifest.json"
    bm_path.write_text(json.dumps(build_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # SHA256SUMS + CRC32SUMS
    sums_path = DIST_DIR / "SHA256SUMS.txt"
    sums_path.write_text(f"{build_manifest['artifact_sha256']}  {exe_path.name}\n", encoding="utf-8")
    crc_path = DIST_DIR / "CRC32SUMS.txt"
    crc_path.write_text(f"{build_manifest['artifact_crc32']}  {exe_path.name}\n", encoding="utf-8")

    size_mb = exe_path.stat().st_size / (1024 * 1024)
    print(f"\n  [OK] {exe_path.name} ({size_mb:.1f} MB)")
    print(f"  SHA256: {build_manifest['artifact_sha256'][:32]}")
    print(f"  CRC32: {build_manifest['artifact_crc32']}")
    if "engine_pack_crc32" in build_manifest:
        print(f"  Embedded Engine Pack CRC32: {build_manifest['engine_pack_crc32']}")

    return exe_path


def main(argv: list[str] | None = None) -> int:
    """入口 — 供薄入口调用。

    :returns: 0 成功, 1 失败。
    """
    parser = argparse.ArgumentParser(description="Build the BiliLiveCut Portable Lite executable")
    parser.add_argument(
        "--without-engine-pack",
        action="store_true",
        help="omit Engine Pack metadata (official GitHub Release mode)",
    )
    args = parser.parse_args(argv)

    try:
        build_exe(without_engine_pack=args.without_engine_pack)
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
    raise SystemExit(main())
