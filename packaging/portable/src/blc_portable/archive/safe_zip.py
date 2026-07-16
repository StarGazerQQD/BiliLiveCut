"""安全 ZIP 模块 — 提供 Payload 和 Engine Pack 共用的 ZIP 安全检查。

必须检查:
- Zip Slip (绝对路径)
- 路径遍历 (..)
- Windows 盘符
- 保留设备名 (CON, NUL, COM1 等)
- 流式解压 (避免大文件整读)
- 压缩炸弹检测 (文件数/总大小/压缩比限制)
"""

from __future__ import annotations

import shutil
import stat
import zipfile
from pathlib import Path
from typing import Generator

# Windows 保留设备名 (不区分大小写)
_WIN_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

# 默认安全限制
DEFAULT_MAX_TOTAL_SIZE = 100 * 1024**3  # 100 GB
DEFAULT_MAX_FILE_COUNT = 100_000
DEFAULT_MAX_SINGLE_FILE = 20 * 1024**3  # 20 GB
DEFAULT_MAX_COMPRESSION_RATIO = 100  # 压缩比 > 100x 视为压缩炸弹
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB


def _is_reserved_name(name: str) -> bool:
    """检查文件名是否为 Windows 保留设备名。

    :param name: 文件名 (不含路径)。
    :returns: 是否为保留名。
    """
    stem = Path(name).stem.upper()
    return stem in _WIN_RESERVED_NAMES


def _safe_relative_path(destination_root: Path, member_name: str) -> Path:
    """安全地计算 ZIP 成员在目标目录中的相对路径。

    检查并拒绝:
    - 绝对路径
    - 包含 .. 的路径
    - Windows 盘符路径
    - UNC 路径

    :param destination_root: 解压根目录。
    :param member_name: ZIP 成员名。
    :returns: 解析后的目标路径。
    :raises ValueError: 路径不安全时。
    """
    # 检查绝对路径
    if member_name.startswith(("/", "\\")):
        raise ValueError(f"Zip Slip: 绝对路径 '{member_name}'")

    # 检查 Windows 盘符
    if len(member_name) > 1 and member_name[1] == ":":
        raise ValueError(f"Zip Slip: Windows 盘符路径 '{member_name}'")

    # 检查 UNC 路径
    if member_name.startswith("\\\\") or member_name.startswith("//"):
        raise ValueError(f"Zip Slip: UNC 路径 '{member_name}'")

    # 检查路径遍历
    parts = member_name.replace("\\", "/").split("/")
    for part in parts:
        if part == "..":
            raise ValueError(f"Zip Slip: 路径遍历 '{member_name}'")
        if _is_reserved_name(part):
            raise ValueError(f"Zip Slip: 保留设备名 '{member_name}'")
        # 尾部点和空格 (Windows 文件系统陷阱)
        if part != part.rstrip(". ") and part not in (".", ".."):
            raise ValueError(f"Zip Slip: 尾部点/空格 '{member_name}'")

    # 安全解析
    target = (destination_root / member_name).resolve()
    resolved_root = destination_root.resolve()

    if not str(target).startswith(str(resolved_root) + "\\") and not str(target).startswith(str(resolved_root) + "/"):
        raise ValueError(f"Zip Slip: 路径越界 '{member_name}' -> {target}")

    return target


def is_symlink(info: zipfile.ZipInfo) -> bool:
    """检查 ZIP 成员是否为符号链接。

    :param info: ZipInfo 对象。
    :returns: True 如果是符号链接。
    """
    # Unix 符号链接: external_attr 高 16 位为 0120000
    return stat.S_ISLNK(info.external_attr >> 16)


def _default_compression(path: str) -> int:
    """根据文件扩展名选择压缩策略。

    - 已压缩格式 (模型权重、二进制): ZIP_STORED
    - 文本文件 (JSON, TXT, 配置): ZIP_DEFLATED

    :param path: 文件路径。
    :returns: ZIP 压缩常量。
    """
    compressed_exts = {".bin", ".pt", ".pth", ".pkl", ".safetensors",
                       ".pyd", ".dll", ".so", ".exe", ".whl", ".zip",
                       ".tar", ".gz", ".bz2", ".7z", ".xz", ".mp4",
                       ".mp3", ".wav", ".flac", ".jpg", ".jpeg", ".png"}
    if Path(path).suffix.lower() in compressed_exts:
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def iter_archive_members(
    zf: zipfile.ZipFile,
    destination_root: Path,
    max_total_size: int = DEFAULT_MAX_TOTAL_SIZE,
    max_file_count: int = DEFAULT_MAX_FILE_COUNT,
    max_single_file: int = DEFAULT_MAX_SINGLE_FILE,
    max_compression_ratio: int = DEFAULT_MAX_COMPRESSION_RATIO,
) -> Generator[tuple[zipfile.ZipInfo, Path], None, None]:
    """安全遍历 ZIP 成员，产生 (info, target_path) 元组。

    执行所有安全检查后 yield。

    :param zf: 打开的 ZipFile。
    :param destination_root: 解压根目录。
    :param max_total_size: 最大总解压大小。
    :param max_file_count: 最大文件数。
    :param max_single_file: 最大单文件大小。
    :param max_compression_ratio: 最大压缩比 (压缩炸弹检测)。
    :yields: (ZipInfo, 目标 Path)。
    :raises ValueError: 安全检查失败时。
    """
    total_uncompressed = 0
    file_count = 0
    visited: set[str] = set()

    for info in zf.infolist():
        if info.is_dir():
            continue

        # 符号链接检查
        if is_symlink(info):
            raise ValueError(f"拒绝符号链接: {info.filename}")

        # 文件数限制
        file_count += 1
        if file_count > max_file_count:
            raise ValueError(f"压缩炸弹: 文件数 {file_count} > {max_file_count}")

        # 单文件大小限制
        if info.file_size > max_single_file:
            raise ValueError(f"单文件过大: {info.filename} ({info.file_size} > {max_single_file})")

        # 总大小限制
        total_uncompressed += info.file_size
        if total_uncompressed > max_total_size:
            raise ValueError(f"压缩炸弹: 总解压大小 {total_uncompressed} > {max_total_size}")

        # 压缩比检测 (跳过存储模式)
        if info.compress_type != zipfile.ZIP_STORED and info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > max_compression_ratio:
                raise ValueError(
                    f"压缩炸弹: {info.filename} 压缩比 {ratio:.1f}x > {max_compression_ratio}x"
                )

        # 路径安全检查
        target = _safe_relative_path(destination_root, info.filename)

        # 重复路径检查
        normalized = str(target.resolve()).lower()
        if normalized in visited:
            raise ValueError(f"重复路径: {info.filename}")
        visited.add(normalized)

        yield info, target


def safe_extract(
    zf: zipfile.ZipFile,
    destination_root: Path,
    max_total_size: int = DEFAULT_MAX_TOTAL_SIZE,
    max_file_count: int = DEFAULT_MAX_FILE_COUNT,
    max_single_file: int = DEFAULT_MAX_SINGLE_FILE,
    max_compression_ratio: int = DEFAULT_MAX_COMPRESSION_RATIO,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> list[str]:
    """安全流式解压 ZIP 文件。

    :param zf: 打开的 ZipFile。
    :param destination_root: 解压根目录。
    :param max_total_size: 最大总解压大小。
    :param max_file_count: 最大文件数。
    :param max_single_file: 最大单文件大小。
    :param max_compression_ratio: 最大压缩比。
    :param chunk_size: 流式读取块大小。
    :returns: 解压的文件路径列表。
    :raises ValueError: 安全检查失败时。
    """
    destination_root.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []

    for info, target in iter_archive_members(
        zf, destination_root,
        max_total_size=max_total_size,
        max_file_count=max_file_count,
        max_single_file=max_single_file,
        max_compression_ratio=max_compression_ratio,
    ):
        target.parent.mkdir(parents=True, exist_ok=True)

        # 流式解压 — 分块读写，避免大文件整读入内存
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, length=chunk_size)

        extracted.append(str(target))

    return extracted


def compute_streaming_hash(zip_path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """流式计算 ZIP 文件的 SHA-256。

    :param zip_path: ZIP 文件路径。
    :param chunk_size: 读取块大小。
    :returns: SHA-256 十六进制字符串。
    """
    import hashlib

    hasher = hashlib.sha256()
    with open(zip_path, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()
