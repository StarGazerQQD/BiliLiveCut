"""Engine Pack 构建脚本 — 下载四引擎模型并打包为独立 ZIP。

用法:
    python build_engine_pack.py              # 真实下载四引擎模型
    python build_engine_pack.py --fixture    # 生成测试用 Fixture (小体积)

输出:
    dist/engine-pack/
    ├── BiliLiveCut-EnginePack-v0.1.14.5-alpha.zip
    ├── engine-pack-manifest.json
    ├── CRC32SUMS.txt
    ├── SHA256SUMS.txt
    └── build-manifest.json

同时生成:
    resources/engine_pack_info.json  (供 PyInstaller 嵌入)
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
import zlib
from pathlib import Path
from typing import Any

# ── 常量 ───────────────────────────────────────────────────

PORTABLE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTABLE_DIR.parent.parent
BUILD_DIR = PORTABLE_DIR / "build" / "engine-pack"
DIST_DIR = PORTABLE_DIR / "dist" / "engine-pack"
RESOURCES_DIR = PORTABLE_DIR / "resources"

ENGINE_PACK_VERSION = "0.1.14.5-alpha"
SOURCE_COMMIT_SHORT = "74c21b4"
ARCHIVE_NAME = f"BiliLiveCut-EnginePack-{ENGINE_PACK_VERSION}"

CHUNK_SIZE = 8 * 1024 * 1024


# ── 四引擎定义 ────────────────────────────────────────────

ENGINES = [
    {
        "engine_id": "whisper",
        "engine_name": "Whisper (兜底引擎)",
        "model_id": "large-v3-turbo",
        "hub": "huggingface",
        "repo_id": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "revision": None,
        "target_path": "models/whisper",
    },
    {
        "engine_id": "paraformer",
        "engine_name": "Paraformer-zh (主引擎)",
        "model_id": "paraformer-zh",
        "hub": "modelscope",
        "revision": "v2.0.4",
        "target_path": "models/paraformer",
        "sub_models": [
            {"model_id": "fsmn-vad", "revision": "v2.0.4"},
            {"model_id": "ct-punc", "revision": "v2.0.4"},
            {"model_id": "cam++", "revision": "v2.0.4"},
        ],
    },
    {
        "engine_id": "sensevoice",
        "engine_name": "SenseVoice-Small (辅助特征)",
        "model_id": "iic/SenseVoiceSmall",
        "hub": "modelscope",
        "revision": "v2.0.4",
        "target_path": "models/sensevoice",
    },
    {
        "engine_id": "funasr_nano",
        "engine_name": "Fun-ASR-Nano (低置信复核)",
        "model_id": "iic/Fun-ASR-Nano",
        "hub": "modelscope",
        "revision": "v2.0.4",
        "target_path": "models/funasr_nano",
    },
]

HF_MIRROR = "https://hf-mirror.com"


# ── 辅助函数 ──────────────────────────────────────────────


def get_full_commit(short: str) -> str:
    """解析完整 Commit Hash。

    :param short: 短 Hash。
    :returns: 完整 Hash。
    :raises RuntimeError: 无法解析时。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", f"{short}^{{commit}}"],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=15,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        raise RuntimeError(f"无法解析 Commit {short}。请确认该 Commit 在本地仓库中。") from None


def compute_crc32(path: Path) -> str:
    """流式计算 CRC32。

    :param path: 文件路径。
    :returns: 8 位大写十六进制 CRC32。
    """
    crc_val: int = 0
    with path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            crc_val = zlib.crc32(chunk, crc_val)
    return f"{crc_val & 0xFFFFFFFF:08X}"


def compute_sha256(path: Path) -> str:
    """流式计算 SHA-256。

    :param path: 文件路径。
    :returns: SHA-256 十六进制。
    """
    import hashlib

    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


# ── 真实下载 ──────────────────────────────────────────────


def download_real_models(staging: Path) -> None:
    """下载四个引擎的真实模型到 staging 目录。

    :param staging: staging 根目录。
    """
    print("=" * 60)
    print("  下载四引擎模型 (完整下载)")
    print("=" * 60)

    for idx, engine in enumerate(ENGINES):
        engine_id = engine["engine_id"]
        target = staging / engine["target_path"]
        target.mkdir(parents=True, exist_ok=True)
        desc = engine.get("engine_name", engine_id)

        print(f"\n  [{idx + 1}/4] {desc}")

        hub = engine["hub"]
        if hub == "huggingface":
            repo_id = engine["repo_id"]
            try:
                from huggingface_hub import snapshot_download

                os.environ["HF_ENDPOINT"] = HF_MIRROR
                os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
                snapshot_download(
                    repo_id=repo_id,
                    revision=engine["revision"],
                    local_dir=str(target),
                    local_dir_use_symlinks=False,
                    resume_download=True,
                )
            except ImportError:
                raise ImportError("需要安装 huggingface_hub: pip install huggingface_hub") from None

        elif hub == "modelscope":
            try:
                from modelscope.hub.snapshot_download import snapshot_download

                model_id = engine["model_id"]
                revision = engine["revision"]
                snapshot_download(model_id=model_id, revision=revision, local_dir=str(target))

                # 子模型 (Paraformer)
                for sub in engine.get("sub_models", []):
                    sub_id = sub["model_id"]
                    sub_rev = sub["revision"]
                    sub_dir = target / sub_id
                    sub_dir.mkdir(parents=True, exist_ok=True)
                    print(f"    子模型: {sub_id}")
                    snapshot_download(model_id=sub_id, revision=sub_rev, local_dir=str(sub_dir))
            except ImportError:
                raise ImportError("需要安装 modelscope: pip install modelscope") from None

        fc = sum(1 for _ in target.rglob("*") if _.is_file())
        ts = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        print(f"    文件: {fc}, 大小: {ts / (1024**3):.2f} GB")


# ── Fixture 生成 ─────────────────────────────────────────


def build_fixture(staging: Path) -> None:
    """生成测试用小型 Fixture (每个引擎一个占位文件)。

    :param staging: staging 根目录。
    """
    print("  生成 Fixture Engine Pack (测试用) ...")
    for engine in ENGINES:
        target = staging / engine["target_path"]
        target.mkdir(parents=True, exist_ok=True)
        meta = {
            "engine_id": engine["engine_id"],
            "model_id": engine["model_id"],
            "revision": engine.get("revision"),
            "_fixture": True,
        }
        (target / "model_metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (target / "README.txt").write_text(
            f"Fixture placeholder for {engine['engine_name']}\n",
            encoding="utf-8",
        )
        # Paraformer 子模型占位
        for sub in engine.get("sub_models", []):
            sub_dir = target / sub["model_id"]
            sub_dir.mkdir(parents=True, exist_ok=True)
            (sub_dir / "model_metadata.json").write_text(
                json.dumps({"model_id": sub["model_id"], "_fixture": True}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


# ── ZIP 打包 ─────────────────────────────────────────────


def create_zip(staging: Path, output_path: Path) -> None:
    """将 staging 目录打包为固定顺序的 ZIP (可复现)。

    :param staging: staging 目录。
    :param output_path: 输出 ZIP 路径。
    """
    all_files: list[Path] = []
    for p in sorted(staging.rglob("*")):
        if p.is_file():
            all_files.append(p)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for f in all_files:
            arcname = f.relative_to(staging).as_posix()
            info = zipfile.ZipInfo(arcname)
            # 固定时间戳为 epoch (可复现)
            info.date_time = (2026, 1, 1, 0, 0, 0)
            info.external_attr = 0o644 << 16  # 固定权限
            zf.writestr(info, f.read_bytes())

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\n  ZIP 已创建: {output_path.name} ({size_mb:.1f} MB)")


# ── 生成 Manifest / SUMS / Info ────────────────────────────


def generate_outputs(
    staging: Path,
    archive_path: Path,
    source_commit: str,
    is_fixture: bool,
) -> dict[str, Any]:
    """生成 Manifest、CRC32SUMS、SHA256SUMS、build-manifest 和 engine_pack_info。

    :param staging: staging 目录。
    :param archive_path: ZIP 文件路径。
    :param source_commit: 74c21b4 完整 Hash。
    :param is_fixture: 是否为 Fixture。
    :returns: 构建信息字典。
    """
    # 流式计算 CRC32 和 SHA-256
    crc32_val = compute_crc32(archive_path)
    sha256_val = compute_sha256(archive_path)

    # 构建逐文件清单
    file_list: dict[str, dict[str, object]] = {}
    for p in sorted(staging.rglob("*")):
        if p.is_file():
            rel = p.relative_to(staging).as_posix()
            file_list[rel] = {
                "size": p.stat().st_size,
                "sha256": compute_sha256(p),
            }

    # 生成 Manifest
    manifest: dict[str, Any] = {
        "format_version": 1,
        "engine_pack_version": ENGINE_PACK_VERSION,
        "portable_release_version": ENGINE_PACK_VERSION,
        "source_commit": source_commit,
        "source_commit_short": SOURCE_COMMIT_SHORT,
        "archive_filename": archive_path.name,
        "archive_crc32": crc32_val,
        "archive_sha256": sha256_val,
        "total_files": len(file_list),
        "fixture": is_fixture,
        "engines": [
            {
                "engine_id": e["engine_id"],
                "engine_name": e["engine_name"],
                "model_id": e["model_id"],
                "hub": e["hub"],
                "revision": e.get("revision"),
                "target_path": e["target_path"],
                "model_repo": e.get("repo_id"),
                "sub_models": e.get("sub_models", []),
            }
            for e in ENGINES
        ],
        "files": file_list,
    }

    DIST_DIR.mkdir(parents=True, exist_ok=True)

    # 写入 Manifest
    manifest_path = DIST_DIR / "engine-pack-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # CRC32SUMS
    (DIST_DIR / "CRC32SUMS.txt").write_text(f"{crc32_val}  {archive_path.name}\n", encoding="utf-8")

    # SHA256SUMS
    (DIST_DIR / "SHA256SUMS.txt").write_text(f"{sha256_val}  {archive_path.name}\n", encoding="utf-8")

    # build-manifest.json
    build_manifest: dict[str, Any] = {
        "release_version": ENGINE_PACK_VERSION,
        "source_commit": source_commit,
        "source_commit_short": SOURCE_COMMIT_SHORT,
        "architecture": "x64",
        "artifact_type": "engine-pack",
        "fixture": is_fixture,
        "archive_crc32": crc32_val,
        "archive_sha256": sha256_val,
        "file_count": len(file_list),
        "built_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (DIST_DIR / "build-manifest.json").write_text(
        json.dumps(build_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # engine_pack_info.json (供 PyInstaller 嵌入)
    RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    engine_pack_info: dict[str, Any] = {
        "engine_pack_version": ENGINE_PACK_VERSION,
        "filename": archive_path.name,
        "crc32": crc32_val,
        "expected_engine_ids": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
    }
    (RESOURCES_DIR / "engine_pack_info.json").write_text(
        json.dumps(engine_pack_info, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total_size = archive_path.stat().st_size
    print(f"\n  Manifest:     {manifest_path}")
    print(f"  CRC32:        {crc32_val}")
    print(f"  SHA-256:      {sha256_val[:32]}...")
    print(f"  Total files:  {len(file_list)}")
    print(f"  Size:         {total_size / (1024**3):.2f} GB")

    if total_size > 4 * 1024**3:
        print("  ⚠ ZIP 超过 4GB，请使用 NTFS/exFAT 文件系统。FAT32 不支持单文件 >4GB。")

    return build_manifest


# ── 自校验 ────────────────────────────────────────────────


def self_verify(archive_path: Path, manifest: dict[str, Any]) -> bool:
    """自校验：解压 Engine Pack 并验证 Manifest。

    :param archive_path: ZIP 路径。
    :param manifest: Manifest 字典。
    :returns: True 通过。
    """
    verify_dir = BUILD_DIR / f"verify-{uuid.uuid4().hex[:8]}"
    verify_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 解压
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(verify_dir)

        # 校验引擎目录
        for engine in manifest["engines"]:
            ep = verify_dir / engine["target_path"]
            if not ep.exists():
                print(f"  [FAIL] 缺少引擎目录: {engine['target_path']}")
                return False
            if not any(ep.iterdir()):
                print(f"  [FAIL] 引擎目录为空: {engine['target_path']}")
                return False

        print("  自校验通过")
        return True
    finally:
        shutil.rmtree(str(verify_dir), ignore_errors=True)


# ── 主入口 ────────────────────────────────────────────────


def build_engine_pack(fixture: bool = False) -> dict[str, Any]:
    """构建 Engine Pack。

    :param fixture: True 生成测试 Fixture，False 真实下载。
    :returns: 构建信息字典。
    """
    print("=" * 60)
    print(f"  BiliLiveCut Engine Pack {ENGINE_PACK_VERSION}")
    if fixture:
        print("  [Fixture 模式]")
    print("=" * 60)

    # 解析 Commit
    source_commit = get_full_commit(SOURCE_COMMIT_SHORT)
    print(f"\n  Source Commit: {source_commit}")

    # 清理并创建 build 目录
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    staging = BUILD_DIR / "staging"
    if staging.exists():
        shutil.rmtree(str(staging))
    staging.mkdir(parents=True, exist_ok=True)

    # 下载 / 生成模型
    if fixture:
        build_fixture(staging)
    else:
        download_real_models(staging)

    # 打包 ZIP
    archive_path = DIST_DIR / ARCHIVE_NAME
    archive_path = archive_path.with_suffix(".zip")
    create_zip(staging, archive_path)

    # 生成输出
    result = generate_outputs(staging, archive_path, source_commit, is_fixture=fixture)

    # 自校验
    print("\n  执行自校验 ...")
    manifest = json.loads((DIST_DIR / "engine-pack-manifest.json").read_text(encoding="utf-8"))
    if not self_verify(archive_path, manifest):
        print("\n  [FAIL] 自校验失败!")
        sys.exit(1)

    # 清理 build 临时文件
    shutil.rmtree(str(staging), ignore_errors=True)

    print("\n  [OK] Engine Pack 构建完成")
    print(f"  {archive_path}")
    return result


if __name__ == "__main__":
    fixture_mode = "--fixture" in sys.argv
    build_engine_pack(fixture=fixture_mode)
