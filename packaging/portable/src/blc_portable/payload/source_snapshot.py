"""源码快照提取器 — 从固定 Git Commit 提取业务源码。

使用 git archive 提取当前发布基线的源码，禁止从当前工作区直接复制。
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .manifest import (
    RELEASE_VERSION,
    SOURCE_COMMIT_FULL,
    SOURCE_COMMIT_SHORT,
)

_logger = logging.getLogger(__name__)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[5]

# 需要从 Commit 中提取的文件/目录
PAYLOAD_ITEMS = [
    "app/",
    "config/",
    "pyproject.toml",
    "setup.py",
    "setup_c.py",
    ".env.example",
    "LICENSE",
]

# 禁止进入 Payload 的路径模式
EXCLUDE_PATTERNS = [
    ".git",
    ".github",
    "tests/",
    "docs/",
    "__pycache__/",
    "*.pyc",
    "storage/",
    ".env",
    ".venv/",
    "build/",
    "dist/",
    "models/",
    "vendor/",
    "bin/",
    "*.log",
    "*.db",
    "*.sqlite3",
    "*.egg-info/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".audit_cache/",
    ".vscode/",
    ".idea/",
    ".DS_Store",
    "Thumbs.db",
    ".git_msg.txt",
    ".gitignore",
]


def _git_path_list(command: list[str]) -> list[str]:
    """运行只读 Git 路径查询并返回规范化的相对路径。"""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(_REPOSITORY_ROOT),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"无法核对 Payload 源码基线: {exc}") from exc

    if result.returncode != 0:
        diagnostic = result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"
        raise RuntimeError(f"无法核对 Payload 源码基线: {diagnostic}")

    return sorted({line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()})


def verify_workspace_source_baseline(source_commit: str) -> None:
    """拒绝构建落后于当前业务源码的 Payload 基线。

    构建工具和发布元数据可以位于源码基线之后，但任何会进入 Payload
    的业务文件都必须与固定提交一致；否则 ``git archive`` 会静默丢失
    已提交或未提交的修复。

    :param source_commit: 当前配置的业务源码完整 Commit Hash。
    :raises RuntimeError: Git 查询失败或业务源码与固定基线不一致。
    """
    changed = _git_path_list(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "diff",
            "--name-only",
            "--diff-filter=ACDMRTUXB",
            source_commit,
            "--",
            *PAYLOAD_ITEMS,
        ]
    )
    untracked = _git_path_list(
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            *PAYLOAD_ITEMS,
        ]
    )
    stale_paths = sorted(set(changed) | set(untracked))
    if stale_paths:
        preview = ", ".join(stale_paths[:12])
        suffix = f" 等 {len(stale_paths)} 个文件" if len(stale_paths) > 12 else ""
        raise RuntimeError(
            f"Payload 源码基线 {source_commit[:7]} 已落后于当前业务源码: {preview}{suffix}；"
            "请先提交业务修复并更新 source_commit"
        )


def resolve_commit(commit_ref: str) -> str:
    """解析 Commit 引用的完整 40 字符 Hash。

    :param commit_ref: 短 Commit Hash 或引用。
    :returns: 完整 40 字符 Hash。
    :raises RuntimeError: Commit 不存在时。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", commit_ref],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            cwd=str(_REPOSITORY_ROOT),
        )
        full_hash = result.stdout.strip()
        if len(full_hash) != 40:
            raise RuntimeError(f"Git 返回的 Hash 长度异常: {len(full_hash)} 字符")

        _logger.info("source_commit: short=%s full=%s", SOURCE_COMMIT_SHORT, full_hash)

        # 验证是否是预期的 Commit
        if full_hash != SOURCE_COMMIT_FULL:
            raise RuntimeError(f"Commit Hash 不匹配: resolved={full_hash} expected={SOURCE_COMMIT_FULL}")

        return full_hash
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"无法解析 Commit {commit_ref}: {exc.stderr.strip() if exc.stderr else exc}") from exc


def extract_source(commit_ref: str, output_dir: Path) -> dict:
    """从指定 Commit 提取源码到输出目录。

    使用 git archive 提取，不使用当前工作区。

    :param commit_ref: Commit Hash。
    :param output_dir: 输出目录（必须为空或不存在）。
    :returns: 提取报告 dict。
    :raises RuntimeError: 提取失败时。
    """
    full_hash = resolve_commit(commit_ref)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 验证输出目录为空
    existing = list(output_dir.iterdir())
    if existing:
        raise RuntimeError(f"输出目录非空: {output_dir} 包含 {len(existing)} 个条目")

    # 确定仓库根目录（git archive 必须在仓库根运行）
    repo_root_result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(_REPOSITORY_ROOT),
    )
    if repo_root_result.returncode != 0:
        raise RuntimeError("无法确定 Git 仓库根目录")
    repo_root = Path(repo_root_result.stdout.strip())

    # 使用 git archive 提取
    tmp_tar = output_dir / "_archive.tar"

    try:
        cmd = ["git", "-c", "core.autocrlf=false", "archive", "--format=tar", "--output", str(tmp_tar), full_hash]
        _logger.info("extracting: %s (cwd=%s)", " ".join(cmd), repo_root)
        subprocess.run(cmd, check=True, timeout=30, cwd=str(repo_root))

        if not tmp_tar.exists() or tmp_tar.stat().st_size == 0:
            raise RuntimeError("git archive 未产生有效输出")

        # 解包到 output_dir (安全: 使用 data filter, 拒绝链接和越界)
        import tarfile

        with tarfile.open(tmp_tar) as tar:
            # 逐成员验证, 拒绝路径遍历和符号链接
            for member in tar.getmembers():
                if member.islnk() or member.issym():
                    raise RuntimeError(f"git archive 包含链接: {member.name}")
                # 解析并验证目标路径
                resolved = (output_dir / member.name).resolve()
                try:
                    resolved.relative_to(output_dir.resolve())
                except ValueError:
                    raise RuntimeError(f"路径越界: {member.name} → {resolved}") from None
            tar.extractall(path=output_dir, filter="data")

        # 验证关键文件
        missing = []
        for item in ["app/cli.py", "pyproject.toml"]:
            if not (output_dir / item).exists():
                missing.append(item)
        if missing:
            raise RuntimeError(f"提取后缺失关键文件: {', '.join(missing)}")

        # 生成报告
        file_count = sum(1 for _ in output_dir.rglob("*") if _.is_file())
        report: dict = {
            "source_commit_short": SOURCE_COMMIT_SHORT,
            "source_commit_full": full_hash,
            "output_dir": str(output_dir),
            "file_count": file_count,
            "files": sorted(str(p.relative_to(output_dir).as_posix()) for p in output_dir.rglob("*") if p.is_file()),
            "extracted_at": datetime.now(UTC).isoformat(),
        }

        _logger.info("extracted: %d files from %s", file_count, full_hash[:8])
        return report

    except tarfile.TarError as exc:
        raise RuntimeError(f"解包 git archive 失败: {exc}") from exc
    finally:
        if tmp_tar.exists():
            tmp_tar.unlink()


def validate_release_identity(staging_dir: Path) -> None:
    """确认源码快照本身已经声明当前发布版本，不修改快照内容。"""
    expected = {
        "app/__init__.py": f'__version__ = "{RELEASE_VERSION}"',
        "pyproject.toml": f'version = "{RELEASE_VERSION}"',
        "setup_c.py": f'version="{RELEASE_VERSION.split("-", maxsplit=1)[0]}"',
    }
    mismatches: list[str] = []
    for rel_path, marker in expected.items():
        path = staging_dir / rel_path
        if not path.is_file() or marker not in path.read_text(encoding="utf-8"):
            mismatches.append(rel_path)
    if mismatches:
        raise RuntimeError(
            f"Payload 源码版本与当前发布版本不一致: {', '.join(mismatches)}；禁止在构建阶段改写旧源码版本"
        )


def verify_source_origin(
    staging_dir: Path,
    source_commit: str,
) -> None:
    """验证 staging 目录中的源码来自指定 Commit。

    业务文件必须与 source_commit 完全一致。

    :param staging_dir: staging 目录。
    :param source_commit: Commit Hash。
    :raises RuntimeError: 文件不一致时。
    """
    business_files = [
        "app/__init__.py",
        "app/cli.py",
        "app/analysis/transcription/backends.py",
        "app/analysis/transcription/pipeline.py",
        "app/web/login_handler.py",
        "app/web/main.py",
        "app/web/static/js/review.js",
        "app/pipeline/workers/analyze.py",
        "app/pipeline/workers/render.py",
        "app/pipeline/workers/publish.py",
        "app/db/entities/highlight.py",
        "app/db/entities/clip.py",
        "app/db/entities/publishing.py",
        "app/pipeline/stale_recovery.py",
        "pyproject.toml",
        "setup_c.py",
    ]

    for rel_path in business_files:
        file_path = staging_dir / rel_path
        if not file_path.exists():
            raise RuntimeError(f"业务文件缺失: {rel_path}")

        try:
            result = subprocess.run(
                ["git", "-c", "core.autocrlf=false", "show", f"{source_commit}:{rel_path.replace(os.sep, '/')}"],
                capture_output=True,
                check=True,
                timeout=10,
                cwd=str(_REPOSITORY_ROOT),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(f"无法从 Commit {source_commit[:8]} 读取业务文件: {rel_path}") from exc

        commit_content = result.stdout.replace(b"\r\n", b"\n")
        staging_content = file_path.read_bytes().replace(b"\r\n", b"\n")
        if commit_content != staging_content:
            raise RuntimeError(f"业务文件 {rel_path} 与 Commit {source_commit[:8]} 不一致 — 源码可能被非受控修改")

    _logger.info("verify_source_origin: all business files match commit %s", source_commit[:8])
