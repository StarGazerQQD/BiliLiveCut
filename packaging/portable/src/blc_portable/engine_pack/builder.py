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
    ├── BiliLiveCut-EnginePack-0.1.15.1-alpha.zip
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
LICENSES_DIR = PORTABLE_DIR / "licenses"

ENGINE_PACK_VERSION = "0.1.15.1-alpha"
SOURCE_COMMIT_SHORT = "1b47a09"
ARCHIVE_NAME = f"BiliLiveCut-EnginePack-{ENGINE_PACK_VERSION}"

CHUNK_SIZE = 8 * 1024 * 1024
MIN_PRODUCTION_ARCHIVE_BYTES = 500 * 1024 * 1024

HF_MIRROR = "https://hf-mirror.com"


# ── 四引擎定义 — 来自统一模型目录 ──────────────────────────
# 所有模型定义现在唯一权威来源: packaging/portable/config/model_sources.lock.json
# 通过 blc_portable.config.model_catalog 统一加载。
# 禁止在此文件或任何其他模块再次定义 ENGINES 常量。

import sys as _sys

_CONFIG_DIR = str(PORTABLE_DIR / "config")
if _CONFIG_DIR not in _sys.path:
    _sys.path.insert(0, _CONFIG_DIR)

from model_catalog import load_engines


def _license_to_dict(license_def: Any) -> dict[str, object]:
    """将许可证定义转换为可写入 Manifest 的字典。"""
    return {
        "name": license_def.name,
        "spdx": license_def.spdx,
        "source": license_def.source,
        "evidence_url": license_def.evidence_url,
        "license_file": license_def.license_file,
        "verified_at": license_def.verified_at,
        "redistribution_verified": license_def.redistribution_verified,
    }


def _get_engines_for_build() -> list[dict[str, Any]]:
    """从模型目录加载引擎定义，转换为构建所需格式。

    使用 resolved_revision（不可变），不使用 requested_revision。
    子模型目录使用 catalog 中显式定义的 target_subdir。

    :returns: 引擎定义列表。
    """
    raw = []
    for e in load_engines():
        d: dict[str, Any] = {
            "engine_id": e.engine_id,
            "engine_name": e.display_name,
            "model_id": e.repo_id if e.hub == "huggingface" else e.repository,
            "hub": e.hub,
            "revision": e.resolved_revision if e.resolved_revision else None,
            "target_path": e.target_path,
            "required_files": e.required_files,
            "license": _license_to_dict(e.license),
        }
        if e.hub == "huggingface":
            d["repo_id"] = e.repository
        if e.sub_models:
            d["sub_models"] = [
                {
                    "model_id": s.repository,
                    "revision": s.resolved_revision if s.resolved_revision else None,
                    "target_subdir": s.target_subdir if s.target_subdir else s.repository.rsplit("/", 1)[-1],
                    "license": _license_to_dict(s.license),
                }
                for s in e.sub_models
            ]
        if e.third_party_components:
            d["third_party_components"] = [
                {
                    "component_id": component.component_id,
                    "display_name": component.display_name,
                    "repository": component.repository,
                    "revision": component.revision,
                    "target_subdir": component.target_subdir,
                    "license": _license_to_dict(component.license),
                }
                for component in e.third_party_components
            ]
        raw.append(d)
    return raw


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


def validate_redistribution_readiness() -> list[str]:
    """校验所有主模型、子模型和随附组件的再分发证据。"""
    errors: list[str] = []

    def validate_license(owner: str, license_data: dict[str, object]) -> None:
        required = ("name", "spdx", "source", "evidence_url", "license_file", "verified_at")
        for key in required:
            if not license_data.get(key):
                errors.append(f"{owner}: license.{key} missing")
        if license_data.get("redistribution_verified") is not True:
            errors.append(f"{owner}: redistribution_verified=false")

        relative_path = Path(str(license_data.get("license_file", "")))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            errors.append(f"{owner}: invalid license_file path")
        elif relative_path.parts and not (PORTABLE_DIR / relative_path).is_file():
            errors.append(f"{owner}: license_file not found: {relative_path.as_posix()}")

    for engine in _get_engines_for_build():
        engine_id = str(engine["engine_id"])
        validate_license(f"engine {engine_id}", dict(engine.get("license", {})))
        for sub_model in engine.get("sub_models", []):
            validate_license(
                f"engine {engine_id} sub-model {sub_model.get('model_id', '?')}",
                dict(sub_model.get("license", {})),
            )
        for component in engine.get("third_party_components", []):
            validate_license(
                f"engine {engine_id} component {component.get('component_id', '?')}",
                dict(component.get("license", {})),
            )

    notice_path = LICENSES_DIR / "THIRD_PARTY_NOTICES.md"
    if not notice_path.is_file():
        errors.append(f"third-party notice not found: {notice_path}")
    return errors


def copy_license_materials(staging: Path) -> None:
    """将第三方许可证与归属声明复制到 Engine Pack。"""
    if not LICENSES_DIR.is_dir():
        raise FileNotFoundError(f"许可证目录不存在: {LICENSES_DIR}")
    shutil.copytree(LICENSES_DIR, staging / "licenses", dirs_exist_ok=True)


def validate_prepared_models(staging: Path) -> list[str]:
    """校验已下载模型是否符合当前锁文件的文件和目录契约。"""
    errors: list[str] = []
    for engine in _get_engines_for_build():
        engine_id = str(engine["engine_id"])
        target = staging / str(engine["target_path"])
        if not target.is_dir():
            errors.append(f"engine {engine_id}: target directory missing: {target}")
            continue

        for required_file in engine.get("required_files", []):
            required_path = target / str(required_file)
            if not required_path.is_file() or required_path.stat().st_size == 0:
                errors.append(f"engine {engine_id}: required file missing or empty: {required_file}")

        for sub_model in engine.get("sub_models", []):
            target_subdir = str(sub_model.get("target_subdir", ""))
            subdir = target / target_subdir
            if not subdir.is_dir() or not any(path.is_file() for path in subdir.rglob("*")):
                errors.append(f"engine {engine_id}: sub-model directory missing or empty: {target_subdir}")

        for component in engine.get("third_party_components", []):
            target_subdir = str(component.get("target_subdir", ""))
            component_dir = target / target_subdir
            if not component_dir.is_dir() or not any(path.is_file() for path in component_dir.rglob("*")):
                errors.append(f"engine {engine_id}: component directory missing or empty: {target_subdir}")

    return errors


# ── 真实下载 ──────────────────────────────────────────────


def download_real_models(staging: Path) -> None:
    """下载四个引擎的真实模型到 staging 目录。

    每个引擎的 revision 可能为 None（使用默认分支），
    已在模型目录中定义。

    :param staging: staging 根目录。
    """
    print("=" * 60)
    print("  下载四引擎模型 (完整下载)")
    print("=" * 60)

    engines_list = _get_engines_for_build()
    for idx, engine in enumerate(engines_list):
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

                # 子模型 (Paraformer) — 使用显式 target_subdir
                for sub in engine.get("sub_models", []):
                    sub_id = str(sub["model_id"])
                    sub_rev = sub.get("revision")
                    sub_dir_name = sub.get("target_subdir", sub_id.rsplit("/", 1)[-1])
                    sub_dir = target / sub_dir_name
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
    for engine in _get_engines_for_build():
        target = staging / str(engine["target_path"])
        target.mkdir(parents=True, exist_ok=True)
        meta = {
            "engine_id": engine["engine_id"],
            "model_id": engine["model_id"],
            "revision": engine.get("revision"),
            "_fixture": True,
        }
        (target / "model_metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        (target / "README.txt").write_text(
            f"Fixture placeholder for {engine['engine_name']}\n",
            encoding="utf-8",
        )
        # Paraformer 子模型占位
        for sub in engine.get("sub_models", []):
            sub_id = str(sub["model_id"])
            sub_name = str(sub.get("target_subdir") or sub_id.rsplit("/", 1)[-1])
            sub_dir = target / sub_name
            sub_dir.mkdir(parents=True, exist_ok=True)
            (sub_dir / "model_metadata.json").write_text(
                json.dumps({"model_id": sub_id, "_fixture": True}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        for component in engine.get("third_party_components", []):
            component_dir = target / str(component["target_subdir"])
            component_dir.mkdir(parents=True, exist_ok=True)
            (component_dir / "component_metadata.json").write_text(
                json.dumps(
                    {
                        "component_id": component["component_id"],
                        "repository": component["repository"],
                        "revision": component["revision"],
                        "_fixture": True,
                    },
                    ensure_ascii=False,
                    indent=2,
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
            from blc_portable.archive.safe_zip import _default_compression

            info.compress_type = _default_compression(arcname)
            with zf.open(info, "w") as dest, f.open("rb") as src:
                while chunk := src.read(CHUNK_SIZE):
                    dest.write(chunk)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\n  ZIP 已创建: {output_path.name} ({size_mb:.1f} MB)")


# ── 自校验 ────────────────────────────────────────────────


def self_verify(archive_path: Path, manifest: dict[str, Any]) -> bool:
    """自校验：使用生产 verifier 验证 Engine Pack。

    :param archive_path: ZIP 路径。
    :param manifest: Manifest 字典。
    :returns: True 通过。
    """
    verify_dir = BUILD_DIR / f"verify-{uuid.uuid4().hex[:8]}"
    verify_dir.mkdir(parents=True, exist_ok=True)

    try:
        from .installer import _safe_extract

        _safe_extract(archive_path, verify_dir)

        from .verifier import verify_extracted_tree

        errors = verify_extracted_tree(verify_dir, manifest)
        if errors:
            for e in errors:
                print(f"  [FAIL] {e}")
            return False

        print("  自校验通过")
        return True
    finally:
        shutil.rmtree(str(verify_dir), ignore_errors=True)


def validate_production_metadata(
    crc32_val: str,
    sha256_val: str,
    manifest_sha: str,
    model_lock_sha: str,
    builder_head: str,
    total_size: int,
) -> list[str]:
    """校验正式 Engine Pack 外部元数据和最小体积。"""
    errors: list[str] = []
    if not crc32_val or len(crc32_val) != 8:
        errors.append(f"CRC32 invalid: {crc32_val}")
    if not sha256_val or len(sha256_val) != 64:
        errors.append(f"SHA-256 invalid: {sha256_val}")
    if not manifest_sha or len(manifest_sha) != 64:
        errors.append(f"content_manifest_sha256 invalid: {manifest_sha}")
    if not model_lock_sha or len(model_lock_sha) != 64:
        errors.append(f"model_lock_sha256 invalid: {model_lock_sha}")
    if not builder_head or len(builder_head) != 40:
        errors.append(f"builder_commit invalid: {builder_head}")
    if total_size < MIN_PRODUCTION_ARCHIVE_BYTES:
        errors.append(f"production archive too small: {total_size} bytes (minimum {MIN_PRODUCTION_ARCHIVE_BYTES})")
    return errors


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
    :param source_commit: 当前发布基线的完整 Hash。
    :param file_list: 逐文件清单。
    :param is_fixture: 是否为 Fixture。
    :returns: build_manifest 字典。
    """
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "schema_version": 4,
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
                "license": e["license"],
                "model_repo": e.get("repo_id"),
                "sub_models": e.get("sub_models", []),
                "third_party_components": e.get("third_party_components", []),
            }
            for e in _get_engines_for_build()
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

    # engine_pack_info.json (供 PyInstaller 嵌入) — 包含完整元数据
    RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    total_size = archive_path.stat().st_size
    manifest_sha = compute_sha256(DIST_DIR / "engine-pack-manifest.json")
    # builder_commit = current HEAD (not fixed source commit)
    builder_head = ""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=10
        )
        if r.returncode == 0:
            builder_head = r.stdout.strip()
    except Exception:
        pass

    model_lock_path = PORTABLE_DIR / "config" / "model_sources.lock.json"
    model_lock_sha = compute_sha256(model_lock_path) if model_lock_path.exists() else ""

    # Load version.json for schema version cross-references
    version_json_path = PORTABLE_DIR / "config" / "version.json"
    engine_pack_api_version = 4
    model_set_version = 4
    payload_schema_version = 4
    if version_json_path.exists():
        vj = json.loads(version_json_path.read_text(encoding="utf-8"))
        engine_pack_api_version = int(vj.get("engine_pack_schema", 4))
        model_set_version = int(vj.get("model_lock_schema", 4))
        payload_schema_version = int(vj.get("payload_schema", 4))

    # artifact_class 必须显式存在 — 禁止缺失后默认为 production
    artifact_class = "fixture" if is_fixture else "production"

    engine_pack_info: dict[str, Any] = {
        "format_version": 4,
        "artifact_class": artifact_class,
        "engine_pack_version": ENGINE_PACK_VERSION,
        "engine_pack_api_version": engine_pack_api_version,
        "model_set_version": model_set_version,
        "payload_schema_version": payload_schema_version,
        "compatible_app": {"min": ENGINE_PACK_VERSION, "max_exclusive": "0.1.16"},
        "filename": archive_path.name,
        "size_bytes": total_size,
        "crc32": crc32_val,
        "sha256": sha256_val,
        "content_manifest_sha256": manifest_sha,
        "model_lock_sha256": model_lock_sha,
        "source_commit": source_commit,
        "builder_commit": builder_head,
        "build_timestamp": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "expected_engine_ids": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
    }

    # Production validation: reject empty hash fields
    if not is_fixture:
        errors = validate_production_metadata(
            crc32_val,
            sha256_val,
            manifest_sha,
            model_lock_sha,
            builder_head,
            total_size,
        )
        if errors:
            raise RuntimeError("Engine Pack production metadata validation FAILED:\n  " + "\n  ".join(errors))
    (RESOURCES_DIR / "engine_pack_info.json").write_text(
        json.dumps(engine_pack_info, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n  Manifest:     {manifest_path}")
    print(f"  CRC32:        {crc32_val}")
    print(f"  SHA-256:      {sha256_val[:32]}...")
    print(f"  Total files:  {len(file_list)}")
    print(f"  Size:         {total_size / (1024**3):.2f} GB")

    if total_size > 4 * 1024**3:
        print("  [WARN] ZIP 超过 4GB，请使用 NTFS/exFAT 文件系统。FAT32 不支持单文件 >4GB。")

    return build_manifest


def copy_from_cache(staging: Path) -> None:
    """从持久模型缓存 (.model_cache/) 复制引擎到 staging 目录。

    :param staging: staging 根目录。
    :raises FileNotFoundError: 缓存目录缺失时。
    """
    cache_dir = PORTABLE_DIR / ".model_cache"

    if not cache_dir.exists():
        raise FileNotFoundError(f"模型缓存目录不存在: {cache_dir}\n请先运行: python download_engines.py")

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

        print(f"    {cache_name}: {fc} 文件, {ts / (1024**3):.2f} GB")


# ── 主入口 ────────────────────────────────────────────────


def build_engine_pack(fixture: bool = False, from_cache: bool = False) -> dict[str, Any]:
    """构建 Engine Pack。

    支持三种模式:
    - 默认: 直接下载模型到 staging
    - --fixture: 生成测试用 Fixture
    - --from-cache: 从 .model_cache/ 复制已下载的模型 (需先运行 download_engines.py)
    1. 下载模型 → staging → 第一次打包 → 计算 CRC32/SHA256
    2. 写入完整 Manifest 到 staging → 第二次打包 (含 Manifest)
    3. 生成 dist/ 输出 → 自校验

    :param fixture: True 生成测试 Fixture，False 真实下载。
    :param from_cache: 从缓存复制。
    :returns: 构建信息字典。
    """
    print("=" * 60)
    print(f"  BiliLiveCut Engine Pack {ENGINE_PACK_VERSION}")
    if fixture:
        print("  [Fixture 模式]")
    elif from_cache:
        print("  [缓存模式 — 从 build/model_cache/ 复制]")
    else:
        print("  [Production 模式]")
    print("=" * 60)

    # ── Production redistribution gate ──
    if not fixture:
        redistribution_errors = validate_redistribution_readiness()
        if redistribution_errors:
            raise RuntimeError(
                "Production build blocked by redistribution validation:\n  " + "\n  ".join(redistribution_errors)
            )

    # 解析 Commit
    source_commit = get_full_commit(SOURCE_COMMIT_SHORT)
    print(f"\n  Source Commit: {source_commit}")

    # 当前 builder HEAD (不是固定源码 commit)
    builder_head = ""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=10
        )
        if r.returncode == 0:
            builder_head = r.stdout.strip()
    except Exception:
        pass

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

    if not fixture:
        model_errors = validate_prepared_models(staging)
        if model_errors:
            raise RuntimeError("Prepared model validation FAILED:\n  " + "\n  ".join(model_errors))

    copy_license_materials(staging)

    # ── 阶段 2: 构建文件清单 → 写入 Manifest (不含自身归档哈希) ──
    print("\n  [阶段 2/3] 构建文件清单 → 写入 Manifest ...")

    file_list = build_file_list(staging)

    manifest_data: dict[str, Any] = {
        "schema_version": 4,
        "engine_pack_version": ENGINE_PACK_VERSION,
        "compatible_app": {"min": ENGINE_PACK_VERSION, "max_exclusive": "0.1.16"},
        "source_commit": source_commit,
        "source_commit_short": SOURCE_COMMIT_SHORT,
        "builder_commit": builder_head,
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
                "license": e["license"],
                "model_repo": e.get("repo_id"),
                "sub_models": e.get("sub_models", []),
                "third_party_components": e.get("third_party_components", []),
            }
            for e in _get_engines_for_build()
        ],
        "files": file_list,
    }
    # 注意: 内部 Manifest 不包含 archive_crc32/archive_sha256 (避免自引用问题)。
    #       最终 ZIP 的外部哈希保存在 engine_pack_info.json 和 SHA256SUMS.txt 中。
    (staging / "engine-pack-manifest.json").write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ── 阶段 3: 一次性打包 → 计算最终哈希 → 生成输出 ──
    print("\n  [阶段 3/3] 打包 ZIP → 计算哈希 → 生成输出 ...")
    create_zip(staging, archive_path)

    final_crc32 = compute_crc32(archive_path)
    final_sha256 = compute_sha256(archive_path)
    print(f"  最终 CRC32: {final_crc32}")
    print(f"  最终 SHA-256: {final_sha256[:32]}...")

    # ── 生成输出文件 ──
    print("\n  生成输出文件 ...")

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
