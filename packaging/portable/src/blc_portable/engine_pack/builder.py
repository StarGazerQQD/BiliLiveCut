"""Engine Pack 构建脚本 — 下载四引擎模型并打包为独立 ZIP。

用法:
    python build_engine_pack.py              # 真实下载四引擎模型
    python build_engine_pack.py --fixture    # 生成测试用 Fixture (小体积)

两阶段构建流程:
    1. 下载模型 → staging → 第一次打包 (无 Manifest) → 计算 CRC32/SHA256
    2. 写入完整 Manifest 到 staging → 第二次打包 (含 Manifest)
    3. 生成 dist/ 输出文件 → 自校验

输出:
    dist/engine-pack/
    ├── BiliLiveCut-EnginePack-v0.1.14.6-alpha.zip
    ├── engine-pack-manifest.json
    ├── CRC32SUMS.txt
    ├── SHA256SUMS.txt
    └── build-manifest.json

同时生成:
    resources/engine_pack_info.json  (供 PyInstaller 嵌入)
"""

from __future__ import annotations

import datetime
import hashlib
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

PORTABLE_DIR = Path(__file__).resolve().parent.parent.parent.parent
PROJECT_ROOT = PORTABLE_DIR.parent.parent
BUILD_DIR = PORTABLE_DIR / "build" / "engine-pack"
DIST_DIR = PORTABLE_DIR / "dist" / "engine-pack"
RESOURCES_DIR = PORTABLE_DIR / "resources"

ENGINE_PACK_VERSION = "0.1.14.6-alpha"
SOURCE_COMMIT_SHORT = "731a31c"
ARCHIVE_NAME = f"BiliLiveCut-EnginePack-{ENGINE_PACK_VERSION}"

CHUNK_SIZE = 8 * 1024 * 1024

HF_MIRROR = "https://hf-mirror.com"


# ── 四引擎定义 ────────────────────────────────────────────
# 模型 ID 已针对 ModelScope API 进行修正:
#   - Fun-ASR-Nano: iic/Fun-ASR-Nano 不存在 → FunAudioLLM/Fun-ASR-Nano-2512
#   - cam++: iic/speaker_campplus_... → iic/speech_campplus_... (正确前缀)
#   - SenseVoiceSmall / cam++ / FunASR-Nano: v2.0.4 无对应 revision → None

ENGINES: list[dict[str, Any]] = [
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
        "model_id": "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "hub": "modelscope",
        "revision": "v2.0.4",
        "target_path": "models/paraformer",
        "sub_models": [
            {"model_id": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch", "revision": "v2.0.4"},
            {"model_id": "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch", "revision": "v2.0.4"},
            {"model_id": "iic/speech_campplus_sv_zh-cn_16k-common", "revision": None},
        ],
    },
    {
        "engine_id": "sensevoice",
        "engine_name": "SenseVoice-Small (辅助特征)",
        "model_id": "iic/SenseVoiceSmall",
        "hub": "modelscope",
        "revision": None,
        "target_path": "models/sensevoice",
    },
    {
        "engine_id": "funasr_nano",
        "engine_name": "Fun-ASR-Nano (低置信复核)",
        "model_id": "FunAudioLLM/Fun-ASR-Nano-2512",
        "hub": "modelscope",
        "revision": None,
        "target_path": "models/funasr_nano",
    },
]


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
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_file_list(staging: Path) -> dict[str, dict[str, object]]:
    """构建逐文件清单 (size, sha256)。

    :param staging: staging 根目录。
    :returns: 文件清单字典。
    """
    file_list: dict[str, dict[str, object]] = {}
    for p in sorted(staging.rglob("*")):
        if p.is_file():
            rel = p.relative_to(staging).as_posix()
            file_list[rel] = {
                "size": p.stat().st_size,
                "sha256": compute_sha256(p),
            }
    return file_list


# ── 真实下载 ──────────────────────────────────────────────


def download_real_models(staging: Path) -> None:
    """下载四个引擎的真实模型到 staging 目录。

    每个引擎的 revision 可能为 None（使用默认分支），
    已在 ENGINES 定义中根据 ModelScope API 实测结果设定。

    :param staging: staging 根目录。
    """
    print("=" * 60)
    print("  下载四引擎模型 (完整下载)")
    print("=" * 60)

    for idx, engine in enumerate(ENGINES):
        engine_id = str(engine["engine_id"])
        target = staging / str(engine["target_path"])
        target.mkdir(parents=True, exist_ok=True)
        desc = str(engine.get("engine_name", engine_id))

        print(f"\n  [{idx + 1}/4] {desc}")

        hub = str(engine["hub"])
        revision = engine.get("revision")  # str or None

        if hub == "huggingface":
            repo_id = str(engine["repo_id"])
            try:
                from huggingface_hub import snapshot_download

                os.environ["HF_ENDPOINT"] = HF_MIRROR
                os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
                kwargs: dict[str, Any] = {
                    "repo_id": repo_id,
                    "local_dir": str(target),
                    "local_dir_use_symlinks": False,
                    "resume_download": True,
                }
                if revision:
                    kwargs["revision"] = str(revision)
                snapshot_download(**kwargs)
            except ImportError:
                raise ImportError("需要安装 huggingface_hub: pip install huggingface_hub") from None

        elif hub == "modelscope":
            try:
                from modelscope.hub.snapshot_download import snapshot_download

                model_id = str(engine["model_id"])
                kwargs_ms: dict[str, Any] = {"model_id": model_id, "local_dir": str(target)}
                if revision:
                    kwargs_ms["revision"] = str(revision)
                snapshot_download(**kwargs_ms)

                # 子模型 (Paraformer)
                for sub in engine.get("sub_models", []):
                    sub_id = str(sub["model_id"])
                    sub_rev = sub.get("revision")
                    sub_dir = target / sub_id.rsplit("/", 1)[-1]  # 用最后一段作为目录名
                    sub_dir.mkdir(parents=True, exist_ok=True)
                    print(f"    子模型: {sub_id}")
                    sub_kwargs: dict[str, Any] = {"model_id": sub_id, "local_dir": str(sub_dir)}
                    if sub_rev:
                        sub_kwargs["revision"] = str(sub_rev)
                    snapshot_download(**sub_kwargs)
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
        target = staging / str(engine["target_path"])
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
            sub_id = str(sub["model_id"])
            sub_name = sub_id.rsplit("/", 1)[-1]
            sub_dir = target / sub_name
            sub_dir.mkdir(parents=True, exist_ok=True)
            (sub_dir / "model_metadata.json").write_text(
                json.dumps(
                    {"model_id": sub_id, "_fixture": True}, ensure_ascii=False, indent=2
                ),
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
            info.date_time = (2026, 1, 1, 0, 0, 0)
            info.external_attr = 0o644 << 16
            # 流式写入，CHUNK_SIZE 分块读入
            with zf.open(info, "w") as dest, f.open("rb") as src:
                while chunk := src.read(CHUNK_SIZE):
                    dest.write(chunk)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\n  ZIP 已创建: {output_path.name} ({size_mb:.1f} MB)")


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
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(verify_dir)

        for engine in manifest["engines"]:
            ep = verify_dir / str(engine["target_path"])
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


# ── 输出文件 ──────────────────────────────────────────────


def write_output_files(
    crc32_val: str,
    sha256_val: str,
    archive_path: Path,
    source_commit: str,
    file_list: dict[str, dict[str, object]],
    is_fixture: bool,
) -> dict[str, Any]:
    """写入 dist/ 下的所有输出文件。

    :param crc32_val: CRC32 值。
    :param sha256_val: SHA-256 值。
    :param archive_path: ZIP 文件路径。
    :param source_commit: 731a31c 完整 Hash。
    :param file_list: 逐文件清单。
    :param is_fixture: 是否为 Fixture。
    :returns: build_manifest 字典。
    """
    DIST_DIR.mkdir(parents=True, exist_ok=True)

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

    manifest_path = DIST_DIR / "engine-pack-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    (DIST_DIR / "CRC32SUMS.txt").write_text(f"{crc32_val}  {archive_path.name}\n", encoding="utf-8")
    (DIST_DIR / "SHA256SUMS.txt").write_text(f"{sha256_val}  {archive_path.name}\n", encoding="utf-8")

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
        print("  [WARN] ZIP 超过 4GB，请使用 NTFS/exFAT 文件系统。FAT32 不支持单文件 >4GB。")

    return build_manifest


def copy_from_cache(staging: Path) -> None:
    """从持久模型缓存 (build/model_cache/) 复制引擎到 staging 目录。

    :param staging: staging 根目录。
    :raises FileNotFoundError: 缓存目录缺失时。
    """
    cache_dir = PORTABLE_DIR / ".model_cache"

    if not cache_dir.exists():
        raise FileNotFoundError(
            f"模型缓存目录不存在: {cache_dir}\n请先运行: python download_engines.py"
        )

    print("  从缓存复制模型 ...")
    engine_targets = {
        "whisper": ("whisper", "models/whisper"),
        "paraformer": ("paraformer", "models/paraformer"),
        "sensevoice": ("sensevoice", "models/sensevoice"),
        "funasr_nano": ("funasr_nano", "models/funasr_nano"),
    }

    for cache_name, (cache_subdir, target_rel) in engine_targets.items():
        src = cache_dir / cache_subdir
        dst = staging / target_rel
        if not src.exists() or not any(src.iterdir()):
            print(f"    警告: {cache_name} 缓存为空，跳过")
            continue

        dst.mkdir(parents=True, exist_ok=True)

        # 使用 copytree 复制 (robocopy 在 Windows 上可能更快但需要额外处理)
        fc = 0
        ts = 0
        for f in src.rglob("*"):
            if f.is_file():
                rel = f.relative_to(src)
                dst_file = dst / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(f), str(dst_file))
                fc += 1
                ts += f.stat().st_size

        print(f"    {cache_name}: {fc} 文件, {ts/(1024**3):.2f} GB")


# ── 主入口 ────────────────────────────────────────────────


def build_engine_pack(fixture: bool = False, from_cache: bool = False) -> dict[str, Any]:
    """构建 Engine Pack。

    支持三种模式:
    - 默认: 直接下载模型到 staging
    - --fixture: 生成测试用 Fixture
    - --from-cache: 从 build/model_cache/ 复制已下载的模型 (需先运行 download_engines.py)
    1. 下载模型 → staging → 第一次打包 → 计算 CRC32/SHA256
    2. 写入完整 Manifest 到 staging → 第二次打包 (含 Manifest)
    3. 生成 dist/ 输出 → 自校验

    :param fixture: True 生成测试 Fixture，False 真实下载。
    :returns: 构建信息字典。
    """
    print("=" * 60)
    print(f"  BiliLiveCut Engine Pack {ENGINE_PACK_VERSION}")
    if fixture:
        print("  [Fixture 模式]")
    elif from_cache:
        print("  [缓存模式 — 从 build/model_cache/ 复制]")
    print("=" * 60)

    # 解析 Commit
    source_commit = get_full_commit(SOURCE_COMMIT_SHORT)
    print(f"\n  Source Commit: {source_commit}")

    # 清理并创建 build 目录
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    # 使用唯一临时目录避免旧文件锁冲突
    staging = BUILD_DIR / f"staging-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=True)

    archive_path = DIST_DIR / f"{ARCHIVE_NAME}.zip"

    # ── 阶段 1: 准备模型 ──
    if fixture:
        build_fixture(staging)
    elif from_cache:
        copy_from_cache(staging)
    else:
        download_real_models(staging)

    # ── 阶段 2: 第一次打包 (无 Manifest) → 计算 hashes ──
    print("\n  [阶段 2/4] 第一次打包 → 计算 CRC32/SHA-256 ...")
    create_zip(staging, archive_path)
    crc32_pass1 = compute_crc32(archive_path)
    sha256_pass1 = compute_sha256(archive_path)
    print(f"  临时 CRC32: {crc32_pass1}")
    print(f"  临时 SHA-256: {sha256_pass1[:32]}...")

    # ── 阶段 3: 写入 Manifest → 第二次打包 ──
    print("\n  [阶段 3/4] 写入 Manifest 到 staging → 重新打包 ...")

    file_list = build_file_list(staging)

    manifest_data: dict[str, Any] = {
        "format_version": 1,
        "engine_pack_version": ENGINE_PACK_VERSION,
        "portable_release_version": ENGINE_PACK_VERSION,
        "source_commit": source_commit,
        "source_commit_short": SOURCE_COMMIT_SHORT,
        "archive_filename": archive_path.name,
        "archive_crc32": crc32_pass1,
        "archive_sha256": sha256_pass1,
        "total_files": len(file_list),
        "fixture": fixture,
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
    (staging / "engine-pack-manifest.json").write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 第二次打包 (含 Manifest)
    create_zip(staging, archive_path)

    final_crc32 = compute_crc32(archive_path)
    final_sha256 = compute_sha256(archive_path)
    print(f"  最终 CRC32: {final_crc32}")
    print(f"  最终 SHA-256: {final_sha256[:32]}...")

    # ── 阶段 4: 生成 dist/ 输出文件 ──
    print("\n  [阶段 4/4] 生成输出文件 ...")

    manifest_data["archive_crc32"] = final_crc32
    manifest_data["archive_sha256"] = final_sha256

    (DIST_DIR / "engine-pack-manifest.json").write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    file_list["engine-pack-manifest.json"] = {
        "size": (staging / "engine-pack-manifest.json").stat().st_size,
        "sha256": compute_sha256(staging / "engine-pack-manifest.json"),
    }

    build_result = write_output_files(
        crc32_val=final_crc32,
        sha256_val=final_sha256,
        archive_path=archive_path,
        source_commit=source_commit,
        file_list=file_list,
        is_fixture=fixture,
    )

    # ── 自校验 ──
    print("\n  执行自校验 ...")
    if not self_verify(archive_path, manifest_data):
        print("\n  [FAIL] 自校验失败!")
        sys.exit(1)

    # 清理临时 staging 目录
    try:
        shutil.rmtree(str(staging), ignore_errors=True)
    except OSError:
        pass

    print("\n  [OK] Engine Pack 构建完成")
    print(f"  {archive_path}")
    return build_result


def main() -> int:
    """入口 — 供薄入口和独立运行调用。

    :returns: 0 成功, 1 失败。
    """
    try:
        fixture_mode = "--fixture" in sys.argv
        cache_mode = "--from-cache" in sys.argv
        build_engine_pack(fixture=fixture_mode, from_cache=cache_mode)
        return 0
    except SystemExit as e:
        return int(str(e)) if str(e) else 0
    except Exception as exc:
        print(f"[错误] {exc}")
        return 1


if __name__ == "__main__":
    fixture_mode = "--fixture" in sys.argv
    cache_mode = "--from-cache" in sys.argv
    build_engine_pack(fixture=fixture_mode, from_cache=cache_mode)
