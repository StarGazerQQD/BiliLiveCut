"""Engine Pack 本地安装模块 — 查找、校验、解压、原子安装。

职责:
* 查找程序旁边的 Engine Pack ZIP
* 流式计算 CRC32 并与内置值比较
* 安全解压 Engine Pack 到 staging
* 逐文件 SHA-256 校验
* 原子安装四引擎模型到 <app_root>/models/
* 写入 engine-pack-installed.json 安装清单
"""

from __future__ import annotations

import json
import shutil
import uuid
import zipfile
import zlib
from pathlib import Path
from typing import Any

from blc_portable.atomic_fs import replace_with_retry

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB 流式块大小
INSTALLED_MANIFEST_NAME = "engine-pack-installed.json"
INSTALLED_MANIFEST_SCHEMA = 5
_INSTALLED_MANIFEST_FIELDS = {
    "schema_version",
    "engine_pack_version",
    "installation_source",
    "zip_sha256",
    "engine_ids",
    "file_count",
    "total_size_bytes",
    "installed_at",
    "source_commit",
    "files",
}
_ENGINE_FILE_FIELDS = {"target_path", "file_count", "total_size", "files"}


def compute_crc32(path: Path) -> str:
    """流式计算文件 CRC32 (8 位大写十六进制)。

    :param path: 文件路径。
    :returns: CRC32 字符串。
    """
    crc_val: int = 0
    with path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            crc_val = zlib.crc32(chunk, crc_val)
    return f"{crc_val & 0xFFFFFFFF:08X}"


def compute_sha256(path: Path) -> str:
    """流式计算文件 SHA-256。

    :param path: 文件路径。
    :returns: SHA-256 十六进制字符串。
    """
    import hashlib

    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def find_local_engine_pack(
    app_root: Path,
    expected_filename: str,
    user_path: str | None = None,
) -> Path | None:
    """按顺序查找本地 Engine Pack ZIP。

    1. 用户通过参数指定的路径 (优先级最高)
    2. Launcher EXE 所在目录
    3. <app_root>/packages/

    :param app_root: 应用根目录。
    :param expected_filename: 期望的文件名。
    :param user_path: 用户指定路径。
    :returns: 文件路径，未找到返回 None。
    """
    if user_path:
        p = Path(user_path)
        if p.exists() and p.is_file():
            return p
        if p.is_dir():
            exact = p / expected_filename
            if exact.exists():
                return exact
        print(f"  [警告] 用户指定路径不存在: {user_path}")

    candidate = app_root / expected_filename
    if candidate.exists() and candidate.is_file():
        return candidate

    candidate = app_root / "packages" / expected_filename
    if candidate.exists() and candidate.is_file():
        return candidate

    return None


def _safe_extract(zip_path: Path, target_dir: Path) -> None:
    """安全流式解压 ZIP，复用 blc_portable.archive.safe_zip。

    :param zip_path: ZIP 文件路径。
    :param target_dir: 目标目录。
    :raises RuntimeError: 检测到不安全路径或 ZIP 炸弹时。
    """
    from blc_portable.archive.safe_zip import safe_extract

    with zipfile.ZipFile(zip_path) as zf:
        safe_extract(zf, target_dir)


def _read_installed_manifest(models_dir: Path) -> dict[str, Any] | None:
    """读取已安装模型清单。

    :param models_dir: models 目录。
    :returns: 清单字典，未安装返回 None。
    """
    p = models_dir / INSTALLED_MANIFEST_NAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_installed_manifest(
    models_dir: Path,
    engine_pack_version: str,
    engines: list[str],
    files_info: dict[str, dict[str, object]],
    zip_sha256: str | None,
    source_commit: str,
    installation_source: str,
) -> None:
    """原子写入已安装模型清单。

    :param models_dir: models 目录。
    :param engine_pack_version: Engine Pack 版本。
    :param engines: 已安装引擎列表。
    :param files_info: 引擎文件信息。
    :param zip_sha256: ZIP SHA-256；在线下载时为 None。
    :param source_commit: 源码 Commit。
    :param installation_source: ``engine_pack`` 或 ``online_download``。
    """
    import datetime

    info: dict[str, Any] = {
        "schema_version": INSTALLED_MANIFEST_SCHEMA,
        "engine_pack_version": engine_pack_version,
        "installation_source": installation_source,
        "zip_sha256": zip_sha256,
        "engine_ids": engines,
        "file_count": sum(int(f.get("file_count", 0)) for f in files_info.values()),  # type: ignore[arg-type]
        "total_size_bytes": sum(int(f.get("total_size", 0)) for f in files_info.values()),  # type: ignore[arg-type]
        "installed_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "source_commit": source_commit,
        "files": files_info,
    }
    tmp = models_dir / f"{INSTALLED_MANIFEST_NAME}.tmp"
    target = models_dir / INSTALLED_MANIFEST_NAME
    tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    replace_with_retry(tmp, target)


def _collect_files_info(models_dir: Path, engines: list[str]) -> dict[str, dict[str, object]]:
    """对当前安装的全部模型文件生成严格、可重哈希的清单。"""
    result: dict[str, dict[str, object]] = {}
    for engine_id in engines:
        engine_dir = models_dir / engine_id
        file_entries: dict[str, dict[str, object]] = {}
        for path in sorted(engine_dir.rglob("*")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(engine_dir).as_posix()
            file_entries[rel_path] = {
                "size": path.stat().st_size,
                "sha256": compute_sha256(path),
            }
        result[engine_id] = {
            "target_path": f"models/{engine_id}",
            "file_count": len(file_entries),
            "total_size": sum(int(entry["size"]) for entry in file_entries.values()),
            "files": file_entries,
        }
    return result


def _validate_installed_manifest(installed: dict[str, Any], expected_version: str) -> list[str]:
    """验证当前且唯一的已安装模型清单格式。"""
    errors: list[str] = []
    if set(installed) != _INSTALLED_MANIFEST_FIELDS:
        missing = _INSTALLED_MANIFEST_FIELDS - set(installed)
        unknown = set(installed) - _INSTALLED_MANIFEST_FIELDS
        if missing:
            errors.append(f"Installed manifest missing fields: {sorted(missing)}")
        if unknown:
            errors.append(f"Installed manifest unknown fields: {sorted(unknown)}")
        return errors
    if installed["schema_version"] != INSTALLED_MANIFEST_SCHEMA:
        errors.append(
            f"Installed manifest schema mismatch: {installed['schema_version']} != {INSTALLED_MANIFEST_SCHEMA}"
        )
    if installed["engine_pack_version"] != expected_version:
        errors.append(f"Version mismatch: installed={installed['engine_pack_version']} expected={expected_version}")
    source = installed["installation_source"]
    if source not in {"engine_pack", "online_download"}:
        errors.append(f"Invalid installation_source: {source}")
    zip_sha256 = installed["zip_sha256"]
    if source == "engine_pack" and (not isinstance(zip_sha256, str) or len(zip_sha256) != 64):
        errors.append("Engine Pack installation requires a 64-character zip_sha256")
    if source == "online_download" and zip_sha256 is not None:
        errors.append("Online installation zip_sha256 must be null")
    if not isinstance(installed["source_commit"], str) or len(installed["source_commit"]) != 40:
        errors.append("Installed manifest source_commit invalid")
    engine_ids = installed["engine_ids"]
    if not isinstance(engine_ids, list) or any(not isinstance(item, str) or not item for item in engine_ids):
        errors.append("Installed manifest engine_ids invalid")
        return errors
    files = installed["files"]
    if not isinstance(files, dict) or set(files) != set(engine_ids):
        errors.append("Installed manifest files must exactly match engine_ids")
        return errors
    actual_file_count = 0
    actual_total_size = 0
    for engine_id, engine_info in files.items():
        if not isinstance(engine_info, dict) or set(engine_info) != _ENGINE_FILE_FIELDS:
            errors.append(f"Installed manifest files[{engine_id}] fields invalid")
            continue
        if engine_info["target_path"] != f"models/{engine_id}":
            errors.append(f"Installed manifest files[{engine_id}].target_path invalid")
        entries = engine_info["files"]
        if not isinstance(entries, dict) or not entries:
            errors.append(f"Installed manifest files[{engine_id}].files must be non-empty")
            continue
        engine_size = 0
        for rel_path, entry in entries.items():
            if not isinstance(rel_path, str) or not rel_path:
                errors.append(f"Installed manifest files[{engine_id}] contains invalid path")
                continue
            if not isinstance(entry, dict) or set(entry) != {"size", "sha256"}:
                errors.append(f"Installed manifest file entry invalid: {engine_id}/{rel_path}")
                continue
            size = entry["size"]
            sha256 = entry["sha256"]
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                errors.append(f"Installed manifest size invalid: {engine_id}/{rel_path}")
                continue
            if not isinstance(sha256, str) or len(sha256) != 64:
                errors.append(f"Installed manifest sha256 invalid: {engine_id}/{rel_path}")
                continue
            engine_size += size
        if engine_info["file_count"] != len(entries):
            errors.append(f"Installed manifest file_count invalid: {engine_id}")
        if engine_info["total_size"] != engine_size:
            errors.append(f"Installed manifest total_size invalid: {engine_id}")
        actual_file_count += len(entries)
        actual_total_size += engine_size
    if installed["file_count"] != actual_file_count:
        errors.append("Installed manifest aggregate file_count invalid")
    if installed["total_size_bytes"] != actual_total_size:
        errors.append("Installed manifest aggregate total_size_bytes invalid")
    return errors


def check_installed_models(
    models_dir: Path,
    expected_version: str,
    full_rehash: bool = False,
) -> tuple[bool, list[str]]:
    """检查四引擎是否已全部安装且版本匹配。

    快速模式 (full_rehash=False):
    - 校验安装清单存在性和版本
    - 引擎 ID 集合完整
    - 目录存在且非空

    完整重哈希模式 (full_rehash=True):
    - 所有以上检查
    - 逐文件 SHA-256 与安装清单对比
    - 额外文件检测
    - 文件数量检测

    :param models_dir: models 目录。
    :param expected_version: 期望的 Engine Pack 版本。
    :param full_rehash: 是否执行完整 SHA-256 重哈希。
    :returns: (通过, 错误列表)。
    """
    errors: list[str] = []
    installed = _read_installed_manifest(models_dir)
    if installed is None:
        errors.append("engine-pack-installed.json 不存在")
        return False, errors

    errors.extend(_validate_installed_manifest(installed, expected_version))
    if errors:
        return False, errors

    installed_engines = set(installed["engine_ids"])
    expected = {"whisper", "paraformer", "sensevoice", "funasr_nano"}
    if installed_engines != expected:
        missing = expected - installed_engines
        extra = installed_engines - expected
        if missing:
            errors.append(f"Missing engines: {sorted(missing)}")
        if extra:
            errors.append(f"Extra engines: {sorted(extra)}")

    # Check each engine dir is non-empty
    for engine in expected:
        d = models_dir / engine
        if not d.exists():
            errors.append(f"Engine directory missing: {engine}")
        elif not any(d.iterdir()):
            errors.append(f"Engine directory empty: {engine}")

    # ── Full rehash mode ──
    if full_rehash and not errors:
        files_manifest = installed["files"]

        sha_mismatches = 0
        size_mismatches = 0
        extra_files = 0
        max_detail = 5

        manifest_file_set: set[str] = set()
        for engine_id, engine_info in files_manifest.items():
            engine_files = engine_info["files"]
            for rel_path, file_entry in engine_files.items():
                full_rel = f"{engine_id}/{rel_path}"
                manifest_file_set.add(full_rel)

                target = models_dir / full_rel
                if not target.is_file():
                    errors.append(f"Missing: {full_rel}")
                    continue

                expected_sha = file_entry["sha256"]
                expected_size = file_entry["size"]

                actual_size = target.stat().st_size
                if actual_size != expected_size:
                    size_mismatches += 1
                    if size_mismatches <= max_detail:
                        errors.append(f"Size mismatch: {full_rel} expected={expected_size} actual={actual_size}")

                actual_sha = compute_sha256(target)
                if actual_sha != expected_sha:
                    sha_mismatches += 1
                    if sha_mismatches <= max_detail:
                        errors.append(
                            f"SHA-256 mismatch: {full_rel} expected={expected_sha[:16]}... actual={actual_sha[:16]}..."
                        )

        # Check for extra files
        for engine_id in expected:
            engine_dir = models_dir / engine_id
            if not engine_dir.exists():
                continue
            for p in engine_dir.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(models_dir).as_posix()
                    if rel not in manifest_file_set:
                        extra_files += 1
                        if extra_files <= max_detail:
                            errors.append(f"Extra file: {rel}")

        if sha_mismatches:
            errors.append(f"Full rehash: {sha_mismatches} file(s) SHA-256 mismatch")
        if size_mismatches:
            errors.append(f"Full rehash: {size_mismatches} file(s) size mismatch")
        if extra_files:
            errors.append(f"Full rehash: {extra_files} file(s) not in manifest")

    return len(errors) == 0, errors


def install_from_engine_pack(
    app_root: Path,
    pack_path: Path,
    expected_crc32: str,
    expected_sha256: str,
    expected_version: str,
) -> dict[str, Any]:
    """从本地 Engine Pack 安装四引擎模型。

    流程:
    1. 流式 CRC32 校验（快速损坏检测）
    2. 流式 SHA-256 校验（强完整性验证，强制安装条件）
    3. 流式解压到唯一 staging 目录
    4. 校验内部 Manifest (版本、schema、引擎 ID)
    5. 校验 Manifest SHA-256
    6. 校验四个引擎目录和必需文件
    7. 逐文件 SHA-256 校验
    8. 原子替换 models/（含回滚）
    9. 写入安装清单

    :param app_root: 应用根目录。
    :param pack_path: Engine Pack ZIP 路径。
    :param expected_crc32: 内置 CRC32。
    :param expected_sha256: 内置 SHA-256。
    :param expected_version: 期望版本。
    :returns: 安装信息字典。
    :raises RuntimeError: 校验失败。
    """
    # 1. CRC32 快速检测
    actual_crc32 = compute_crc32(pack_path)
    if expected_crc32 and actual_crc32 != expected_crc32:
        raise RuntimeError(f"CRC32 mismatch: expected={expected_crc32} actual={actual_crc32}")

    # 2. SHA-256 强制校验
    actual_sha256 = compute_sha256(pack_path)
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise RuntimeError(f"SHA-256 mismatch: expected={expected_sha256[:16]} actual={actual_sha256[:16]}")

    if not expected_crc32 and not expected_sha256:
        print("  Engine Pack has no external digest; validating its complete internal manifest")
    print(f"  Engine Pack 校验通过: CRC32={actual_crc32} SHA256={actual_sha256[:16]}...")

    from blc_portable.archive.locks import FileLock, get_engine_pack_lock_path

    lock = FileLock(get_engine_pack_lock_path(app_root))
    models_dir = app_root / "models"
    staging_dir = app_root / f"models-staging-{uuid.uuid4().hex[:12]}"

    with lock.acquire(timeout=120):
        try:
            # 2. 安全解压
            print("  解压 Engine Pack ...")
            staging_dir.mkdir(parents=True, exist_ok=True)
            _safe_extract(pack_path, staging_dir)

            # 3. 校验 Manifest
            manifest_path = staging_dir / "engine-pack-manifest.json"
            if not manifest_path.exists():
                raise RuntimeError("Engine Pack 缺少 engine-pack-manifest.json")
            from .manifest import load_manifest

            manifest = load_manifest(manifest_path)
            if manifest.engine_pack_version != expected_version:
                raise RuntimeError(f"Engine Pack 版本不匹配: {manifest.engine_pack_version} != {expected_version}")

            # 4. 四引擎目录存在性
            for engine in manifest.engines:
                ep = staging_dir / engine.target_path
                if not ep.exists() or not any(ep.iterdir()):
                    raise RuntimeError(f"Engine Pack 缺少引擎目录: {engine.target_path}")

            # 5. 逐文件校验 + 多余文件检测 (使用共用 verifier)
            print(f"  逐文件 SHA-256 校验 ({manifest.total_files} 文件) ...")
            from .verifier import verify_extracted_tree

            errors = verify_extracted_tree(staging_dir, manifest)
            if errors:
                raise RuntimeError("Engine Pack 校验失败:\n  " + "\n  ".join(errors))

            # 6. 引擎信息
            installed_engines: list[str] = []
            for engine in manifest.engines:
                installed_engines.append(engine.engine_id)

            # 7. Atomic directory transaction: models.new -> switch -> verify
            print("  Atomic model installation...")
            models_new = app_root / f"models.new-{uuid.uuid4().hex[:12]}"
            backup_dir = None
            try:
                models_new.mkdir(parents=True, exist_ok=True)
                staging_models = staging_dir / "models"
                if staging_models.exists():
                    for sub in staging_models.iterdir():
                        shutil.move(str(sub), str(models_new / sub.name))
                else:
                    for item in staging_dir.iterdir():
                        if item.name in ("engine-pack-manifest.json", "engine-pack-content-manifest.json"):
                            continue
                        if item.is_dir():
                            shutil.move(str(item), str(models_new / item.name))
                shutil.move(str(manifest_path), str(models_new / "engine-pack-content-manifest.json"))
                files_info = _collect_files_info(models_new, installed_engines)
                _write_installed_manifest(
                    models_new,
                    expected_version,
                    installed_engines,
                    files_info,
                    zip_sha256=actual_sha256,
                    source_commit=manifest.source_commit,
                    installation_source="engine_pack",
                )
                if models_dir.exists() and any(models_dir.iterdir()):
                    backup_dir = app_root / f"models.backup-{uuid.uuid4().hex[:8]}"
                    shutil.move(str(models_dir), str(backup_dir))
                elif models_dir.exists():
                    shutil.rmtree(str(models_dir), ignore_errors=True)
                replace_with_retry(models_new, models_dir)
                for engine_id in installed_engines:
                    ep = models_dir / engine_id
                    if not ep.exists() or not any(ep.iterdir()):
                        raise RuntimeError(f"Post-switch verify failed: {engine_id}")
            except Exception:
                if backup_dir and backup_dir.exists():
                    if models_dir.exists():
                        shutil.rmtree(str(models_dir), ignore_errors=True)
                    shutil.move(str(backup_dir), str(models_dir))
                if models_new.exists():
                    shutil.rmtree(str(models_new), ignore_errors=True)
                raise

        except Exception:
            if staging_dir.exists():
                shutil.rmtree(str(staging_dir), ignore_errors=True)
            raise

        shutil.rmtree(str(staging_dir), ignore_errors=True)
        if backup_dir and backup_dir.exists():
            shutil.rmtree(str(backup_dir), ignore_errors=True)

        print("  Engine pack models installed")
        return {
            "source": "engine_pack",
            "method": "local_extract",
            "network_requests": 0,
            "engines": installed_engines,
            "files": files_info,
        }


def install_models_dir_from_staging(
    app_root: Path,
    staging_dir: Path,
    engine_pack_version: str,
    installed_engines: list[str],
) -> bool:
    """将 staging 目录原子替换为 models/ (在线下载后调用)。

    :param app_root: 应用根目录。
    :param staging_dir: 已完成校验的 staging 目录。
    :param engine_pack_version: Engine Pack 版本。
    :param installed_engines: 已安装引擎列表。
    :returns: True 成功, False 失败且已回滚。
    """
    models_dir = app_root / "models"
    backup_dir = None

    try:
        if models_dir.exists() and any(models_dir.iterdir()):
            backup_dir = app_root / f"models-backup-{uuid.uuid4().hex[:8]}"
            shutil.move(str(models_dir), str(backup_dir))

        models_dir.mkdir(parents=True, exist_ok=True)
        for item in staging_dir.iterdir():
            dest = models_dir / item.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(str(dest), ignore_errors=True)
                else:
                    dest.unlink(missing_ok=True)
            shutil.move(str(item), str(dest))

        import sys

        config_dir = str(Path(__file__).resolve().parent.parent.parent.parent / "config")
        if config_dir not in sys.path:
            sys.path.insert(0, config_dir)
        from version_loader import get_source_commit_full

        files_info = _collect_files_info(models_dir, installed_engines)
        _write_installed_manifest(
            models_dir,
            engine_pack_version,
            installed_engines,
            files_info,
            zip_sha256=None,
            source_commit=get_source_commit_full(),
            installation_source="online_download",
        )

        if backup_dir and backup_dir.exists():
            shutil.rmtree(str(backup_dir), ignore_errors=True)

        return True

    except Exception:
        if backup_dir and backup_dir.exists():
            if models_dir.exists():
                shutil.rmtree(str(models_dir), ignore_errors=True)
            shutil.move(str(backup_dir), str(models_dir))
        return False
