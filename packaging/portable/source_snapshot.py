"""源码快照提取器 — 从固定 Git Commit 提取业务源码。

使用 git archive 提取 74c21b4 的源码，禁止从当前工作区直接复制。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from payload_manifest import (
    RELEASE_VERSION,
    SOURCE_COMMIT_FULL,
    SOURCE_COMMIT_SHORT,
)

_logger = logging.getLogger(__name__)

# 需要从 Commit 中提取的文件/目录
PAYLOAD_ITEMS = [
    "app/",
    "config/",
    "pyproject.toml",
    "setup.py",
    "setup_c.py",
    "build_rust.py",
    ".env.example",
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

# 允许的发布元数据覆盖文件
ALLOWED_OVERLAY_FILES = [
    "app/_version.py",
    "app/__init__.py",
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "payload_manifest.json",
]


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
        )
        full_hash = result.stdout.strip()
        if len(full_hash) != 40:
            raise RuntimeError(f"Git 返回的 Hash 长度异常: {len(full_hash)} 字符")

        _logger.info("source_commit: short=%s full=%s", SOURCE_COMMIT_SHORT, full_hash)

        # 验证是否是预期的 Commit
        if full_hash != SOURCE_COMMIT_FULL:
            raise RuntimeError(
                f"Commit Hash 不匹配: resolved={full_hash} expected={SOURCE_COMMIT_FULL}"
            )

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

    # 使用 git archive 提取
    tmp_tar = output_dir / "_archive.tar"

    try:
        cmd = ["git", "-c", "core.autocrlf=false", "archive", "--format=tar",
               "--output", str(tmp_tar), full_hash]
        _logger.info("extracting: %s", " ".join(cmd))
        subprocess.run(cmd, check=True, timeout=30)

        if not tmp_tar.exists() or tmp_tar.stat().st_size == 0:
            raise RuntimeError("git archive 未产生有效输出")

        # 解包到 output_dir
        import tarfile

        with tarfile.open(tmp_tar) as tar:
            tar.extractall(path=output_dir)

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


def apply_version_overlay(staging_dir: Path) -> list[str]:
    """在 staging 目录中应用受控版本覆盖。

    只修改:
    - app/__init__.py: __version__ 和 __version_label__
    - pyproject.toml: version
    - README.md: 版本展示
    - CHANGELOG.md: 添加版本条目

    :param staging_dir: Payload staging 目录。
    :returns: 实际修改的文件列表。
    """
    modified: list[str] = []

    # 1. app/__init__.py
    init_path = staging_dir / "app" / "__init__.py"
    if init_path.exists():
        content = init_path.read_text(encoding="utf-8")
        if '0.1.14.4-alpha' in content or '0.1.14.3-alpha' in content:
            content = content.replace('"0.1.14.4-alpha"', f'"{RELEASE_VERSION}"')
            content = content.replace('"0.1.14.3-alpha"', f'"{RELEASE_VERSION}"')
            content = content.replace('"V0.1.14.4 Alpha"', '"V0.1.14.5 Alpha"')
            content = content.replace('"V0.1.14.3 Alpha"', '"V0.1.14.5 Alpha"')
            init_path.write_text(content, encoding="utf-8")
            modified.append("app/__init__.py")

    # 2. pyproject.toml
    toml_path = staging_dir / "pyproject.toml"
    if toml_path.exists():
        content = toml_path.read_text(encoding="utf-8")
        if '0.1.14.4' in content or '0.1.14.3' in content:
            content = content.replace('version = "0.1.14.4-alpha"', f'version = "{RELEASE_VERSION}"')
            content = content.replace('version = "0.1.14.3-alpha"', f'version = "{RELEASE_VERSION}"')
            toml_path.write_text(content, encoding="utf-8")
            modified.append("pyproject.toml")

    # 3. README.md
    readme_path = staging_dir / "README.md"
    if readme_path.exists():
        content = readme_path.read_text(encoding="utf-8")
        content = content.replace("V0.1.14.4 Alpha", "V0.1.14.5 Alpha")
        content = content.replace("V0.1.14.3 Alpha", "V0.1.14.5 Alpha")
        content = content.replace("0.1.14.4-alpha", RELEASE_VERSION)
        content = content.replace("0.1.14.3-alpha", RELEASE_VERSION)
        readme_path.write_text(content, encoding="utf-8")
        modified.append("README.md")

    # 4. CHANGELOG.md
    changelog_path = staging_dir / "CHANGELOG.md"
    if changelog_path.exists():
        existing = changelog_path.read_text(encoding="utf-8")
        # 替换旧版本号为 0.1.14.5
        existing = existing.replace("0.1.14.4-alpha", RELEASE_VERSION)
        existing = existing.replace("V0.1.14.4 Alpha", "V0.1.14.5 Alpha")
        changelog_path.write_text(existing, encoding="utf-8")
        modified.append("CHANGELOG.md")

    # 5. setup.py
    setup_py = staging_dir / "setup.py"
    if setup_py.exists():
        content = setup_py.read_text(encoding="utf-8")
        content = content.replace('0.1.14.4-alpha', RELEASE_VERSION)
        content = content.replace('0.1.14.3-alpha', RELEASE_VERSION)
        setup_py.write_text(content, encoding="utf-8")
        modified.append("setup.py")

    # 6. setup_c.py
    setup_c = staging_dir / "setup_c.py"
    if setup_c.exists():
        content = setup_c.read_text(encoding="utf-8")
        content = content.replace('"0.1.14.4"', '"0.1.14.5"')
        content = content.replace('"0.1.14.3"', '"0.1.14.5"')
        setup_c.write_text(content, encoding="utf-8")
        modified.append("setup_c.py")

    # 验证只修改了允许的文件
    for f in modified:
        if f not in ALLOWED_OVERLAY_FILES and not f.startswith("setup"):
            raise RuntimeError(f"版本覆盖修改了非允许文件: {f}")

    _logger.info("release_overlay: %d files modified: %s", len(modified), modified)
    return modified


def verify_source_origin(staging_dir: Path, source_commit: str) -> None:
    """验证 staging 目录中的源码来自指定 Commit。

    通过 git show <commit>:<path> 对比关键文件。

    :param staging_dir: staging 目录。
    :param source_commit: Commit Hash。
    :raises RuntimeError: 文件不一致时。
    """
    # 只验证非覆盖的业务文件
    business_files = [
        "app/cli.py",
        "app/pipeline/workers/analyze.py",
        "app/pipeline/workers/render.py",
        "app/pipeline/workers/publish.py",
        "app/db/entities/highlight.py",
        "app/db/entities/clip.py",
        "app/db/entities/publishing.py",
        "app/pipeline/stale_recovery.py",
    ]

    for rel_path in business_files:
        file_path = staging_dir / rel_path
        if not file_path.exists():
            continue

        try:
            result = subprocess.run(
                ["git", "-c", "core.autocrlf=false", "show",
                 f"{source_commit}:{rel_path.replace(os.sep, '/')}"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                continue

            commit_content = result.stdout.replace(b"\r\n", b"\n")
            staging_content = file_path.read_bytes().replace(b"\r\n", b"\n")

            if commit_content != staging_content:
                raise RuntimeError(
                    f"业务文件 {rel_path} 与 Commit {source_commit[:8]} 不一致 — "
                    "源码可能被非受控修改"
                )
        except subprocess.CalledProcessError:
            continue
        except RuntimeError:
            raise
        except Exception:
            pass

    _logger.info("verify_source_origin: all business files match commit %s", source_commit[:8])
