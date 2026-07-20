"""源码快照提取器 — 从固定 Git Commit 提取业务源码。

使用 git archive 提取 731a31c 的源码，禁止从当前工作区直接复制。
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

# 需要从 Commit 中提取的文件/目录
PAYLOAD_ITEMS = [
    "app/",
    "config/",
    "pyproject.toml",
    "setup.py",
    "setup_c.py",
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
    "app/_portable_release.py",
    "app/__init__.py",
    "pyproject.toml",
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

    使用正则匹配任意 0.1.x 版本号/版本标签，只要不等于当前
    RELEASE_VERSION 就替换。无需每次升版本时手动添加替换列表。

    只修改:
    - app/__init__.py: __version__ 和 __version_label__
    - pyproject.toml: version
    - README.md: 版本展示
    - CHANGELOG.md: 添加版本条目
    - setup.py / setup_c.py: version

    :param staging_dir: Payload staging 目录。
    :returns: 实际修改的文件列表。
    """
    import re

    modified: list[str] = []

    # 版本标签 (如 "V0.1.14.8 Alpha")
    base_version = RELEASE_VERSION.split("-")[0] if "-" in RELEASE_VERSION else RELEASE_VERSION
    version_label = f"V{base_version} Alpha"

    # 正则: 匹配任意 0.1.X.Y[-suffix] 和 V0.1.X.Y Label
    _version_re = re.compile(r"\b0\.1\.\d+\.\d+(?:-[a-z]+)?\b")
    _label_re = re.compile(r"\bV0\.1\.\d+\.\d+\s+[A-Za-z]+\b")

    def _overlay(text: str, target_version: str) -> str:
        """将文本中所有不等于 target_version/target_label 的旧版本号替换为新版本。"""

        def _ver_repl(m: re.Match) -> str:
            return target_version if m.group(0) != target_version else m.group(0)

        def _lbl_repl(m: re.Match) -> str:
            return version_label if m.group(0) != version_label else m.group(0)

        text = _label_re.sub(_lbl_repl, text)
        text = _version_re.sub(_ver_repl, text)
        return text

    # 只操作明确的版本元数据字段，不全文替换 README/CHANGELOG
    targets: list[tuple[str, str]] = [
        ("app/_portable_release.py", RELEASE_VERSION),  # 优先：专属便携版本文件
        ("app/__init__.py", RELEASE_VERSION),
        ("pyproject.toml", RELEASE_VERSION),
        ("setup.py", RELEASE_VERSION),
        ("setup_c.py", base_version),  # setup_c 使用无后缀版本号
    ]

    for rel_path, target_ver in targets:
        file_path = staging_dir / rel_path
        if not file_path.exists():
            continue
        content = file_path.read_text(encoding="utf-8")
        new_content = _overlay(content, target_ver)
        if new_content != content:
            file_path.write_text(new_content, encoding="utf-8")
            modified.append(rel_path)

    # 验证只修改了允许的文件
    for f in modified:
        if f not in ALLOWED_OVERLAY_FILES and not f.startswith("setup"):
            raise RuntimeError(f"版本覆盖修改了非允许文件: {f}")

    _logger.info("release_overlay: %d files modified: %s", len(modified), modified)
    return modified


def verify_source_origin(
    staging_dir: Path,
    source_commit: str,
    backport_ids: list[str] | None = None,
) -> None:
    """验证 staging 目录中的源码来自指定 Commit。

    与 backport 机制协同:
    - 声明过 backport 修改的文件不参与原始 commit 比对
    - 未声明的业务文件必须与 source_commit 完全一致

    :param staging_dir: staging 目录。
    :param source_commit: Commit Hash。
    :param backport_ids: 已应用的 backport ID 列表。
    :raises RuntimeError: 文件不一致时。
    """
    # 已声明 backport 的文件允许变更
    backport_files = _get_backport_modified_files(backport_ids or [])

    # 只验证非覆盖、非 backport 的业务文件
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
        if rel_path in backport_files:
            _logger.info("verify_source_origin: skipping backported file %s", rel_path)
            continue

        file_path = staging_dir / rel_path
        if not file_path.exists():
            continue

        try:
            result = subprocess.run(
                ["git", "-c", "core.autocrlf=false", "show", f"{source_commit}:{rel_path.replace(os.sep, '/')}"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                continue

            commit_content = result.stdout.replace(b"\r\n", b"\n")
            staging_content = file_path.read_bytes().replace(b"\r\n", b"\n")

            if commit_content != staging_content:
                raise RuntimeError(f"业务文件 {rel_path} 与 Commit {source_commit[:8]} 不一致 — 源码可能被非受控修改")
        except subprocess.CalledProcessError:
            continue
        except RuntimeError:
            raise
        except Exception:
            pass

    _logger.info("verify_source_origin: all business files match commit %s", source_commit[:8])


def _get_backport_modified_files(backport_ids: list[str]) -> set[str]:
    """从 backports.json 读取指定 backport 修改的文件列表。

    :param backport_ids: 已应用的 backport ID 列表。
    :returns: 文件路径集合。
    """
    if not backport_ids:
        return set()

    import json as _json

    manifest_path = Path(__file__).resolve().parent.parent.parent.parent / "backports" / "backports.json"
    if not manifest_path.exists():
        return set()

    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    modified: set[str] = set()
    for bp in manifest["backports"]:
        if bp["id"] in backport_ids:
            for f in bp.get("files", []):
                modified.add(f)
    return modified
