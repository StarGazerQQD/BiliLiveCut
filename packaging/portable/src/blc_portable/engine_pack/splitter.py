"""Engine Pack 分卷工具 — 将大 ZIP 切割为 1.8 GiB 分卷。

分卷使用原始字节切片，用户可直接用 copy /b 拼接回完整 ZIP。
不依赖 7-Zip 或专有分卷格式。
"""

from __future__ import annotations

import hashlib
import json
import zlib
from pathlib import Path
from typing import Any

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB 流式读取
PART_SIZE_DEFAULT = 1932735283  # 1.8 GiB


def compute_crc32_file(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    """流式计算文件的 CRC32。

    :param path: 文件路径。
    :param chunk_size: 每次读取字节数。
    :returns: 8 位大写十六进制 CRC32。
    """
    crc = 0
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            crc = zlib.crc32(chunk, crc)
    return f"{crc & 0xFFFFFFFF:08X}"


def compute_sha256_file(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    """流式计算文件的 SHA-256。

    :param path: 文件路径。
    :param chunk_size: 每次读取字节数。
    :returns: 64 位十六进制 SHA-256。
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def split_archive(
    archive_path: Path,
    output_dir: Path,
    part_size: int = PART_SIZE_DEFAULT,
) -> list[dict[str, Any]]:
    """将归档文件切分为等大小分卷。

    :param archive_path: 完整 ZIP 路径。
    :param output_dir: 输出目录。
    :param part_size: 每卷大小 (默认 1.8 GiB)。
    :returns: 分卷信息列表 [{"filename": ..., "size": ..., "crc32": ..., "sha256": ...}].
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    total_size = archive_path.stat().st_size
    num_parts = (total_size + part_size - 1) // part_size

    parts: list[dict[str, Any]] = []
    base_name = archive_path.stem

    with archive_path.open("rb") as src:
        for i in range(num_parts):
            part_name = f"{base_name}.part{i + 1:02d}"
            part_path = output_dir / part_name

            with part_path.open("wb") as dst:
                remaining = min(part_size, total_size - i * part_size)
                while remaining > 0:
                    chunk = src.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    dst.write(chunk)
                    remaining -= len(chunk)

            part_info = {
                "index": i + 1,
                "filename": part_name,
                "size": part_path.stat().st_size,
                "crc32": compute_crc32_file(part_path),
                "sha256": compute_sha256_file(part_path),
            }
            parts.append(part_info)
            print(f"  [{i + 1}/{num_parts}] {part_name}: {part_info['size'] / (1024**3):.2f} GiB  CRC32={part_info['crc32']}")

    return parts


def generate_index(
    archive_path: Path,
    parts: list[dict[str, Any]],
    engine_pack_version: int = 1,
    release_tag: str = "asr-engine-pack-v1",
    source_commit: str = "",
    repository: str = "",
) -> dict[str, Any]:
    """生成外部 Engine Pack Index JSON。

    :param archive_path: 完整 ZIP 路径。
    :param parts: 分卷信息列表。
    :param engine_pack_version: Engine Pack 版本号。
    :param release_tag: GitHub Release 标签。
    :param source_commit: 源码 Commit。
    :param repository: GitHub 仓库 (org/repo)。
    :returns: Index 字典。
    """
    return {
        "format_version": 1,
        "engine_pack_version": str(engine_pack_version),
        "compatible_app_version": "0.1.14.6-alpha",
        "repository": repository,
        "release_tag": release_tag,
        "archive_filename": archive_path.name,
        "archive_size": archive_path.stat().st_size,
        "archive_crc32": compute_crc32_file(archive_path),
        "archive_sha256": compute_sha256_file(archive_path),
        "expected_engine_ids": [
            "paraformer",
            "sensevoice",
            "funasr_nano",
            "whisper",
        ],
        "source_commit": source_commit,
        "parts": parts,
    }


def write_index(index: dict[str, Any], output_dir: Path) -> Path:
    """将 Index 写入文件。

    :param index: Index 字典。
    :param output_dir: 输出目录。
    :returns: 写入的文件路径。
    """
    index_path = output_dir / "engine-pack-index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return index_path


def verify_split(archive_path: Path, output_dir: Path, parts: list[dict[str, Any]]) -> bool:
    """验证分卷拼接后与原始文件一致。

    :param archive_path: 原始完整 ZIP。
    :param output_dir: 分卷所在目录。
    :param parts: 分卷信息列表。
    :returns: True 如果拼接后 SHA-256 一致。
    """
    original_hash = compute_sha256_file(archive_path)
    combined_hash = hashlib.sha256()

    for part in parts:
        part_path = output_dir / part["filename"]
        if not part_path.exists():
            print(f"  错误: 分卷 {part['filename']} 不存在")
            return False
        with part_path.open("rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                combined_hash.update(chunk)

    combined_hex = combined_hash.hexdigest()
    if combined_hex != original_hash:
        print(f"  分卷拼接 SHA-256 不匹配!")
        print(f"    原始: {original_hash}")
        print(f"    拼接: {combined_hex}")
        return False

    print(f"  分卷拼接校验通过 SHA-256: {original_hash[:32]}...")
    return True
