"""Payload 文件白名单 — 定义 ZIP 和 Manifest 共用的唯一文件集合。

所有 Payload 构建（snapshot → ZIP → manifest）必须基于此白名单，
确保三个产物的文件集合完全一致。
"""

from __future__ import annotations

from pathlib import Path

# 需从 git archive 提取的目录/文件
PAYLOAD_ITEMS = [
    "app/",
    "config/",
    "pyproject.toml",
    "setup.py",
    "setup_c.py",
    ".env.example",
]

# 禁止进入 Payload 的路径
EXCLUDE_PATTERNS = [
    ".git", ".github", "tests/", "docs/",
    "__pycache__/", "*.pyc", "storage/", ".env",
    ".venv/", "build/", "dist/", "models/",
    "vendor/", "bin/", "*.log", "*.db", "*.sqlite3",
    "*.egg-info/", ".pytest_cache/", ".ruff_cache/",
    ".mypy_cache/", ".audit_cache/", ".vscode/", ".idea/",
    ".DS_Store", "Thumbs.db", ".git_msg.txt", ".gitignore",
]

# 允许的版本注入文件
ALLOWED_OVERLAY_FILES = [
    "app/_version.py",
    "app/__init__.py",
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "payload_manifest.json",
]


def get_payload_file_list(
    staging_dir: Path, *, base_path: Path | None = None
) -> list[str]:
    """生成 Payload 文件清单 (相对于 staging_dir 的路径)。

    :param staging_dir: staging 根目录。
    :param base_path: 用于计算相对路径的基准 (默认 staging_dir)。
    :returns: 排序后的文件路径列表。
    """
    base = base_path or staging_dir
    files: list[str] = []
    for p in sorted(staging_dir.rglob("*")):
        if p.is_file():
            # 检查排除模式
            rel = p.relative_to(staging_dir).as_posix()
            skip = False
            for pat in EXCLUDE_PATTERNS:
                if pat.startswith("*"):
                    if Path(rel).match(pat):
                        skip = True
                        break
                elif pat.endswith("/"):
                    if rel.startswith(pat) or rel == pat[:-1]:
                        skip = True
                        break
                elif rel == pat:
                    skip = True
                    break
            if not skip:
                files.append(p.relative_to(base).as_posix() if base != staging_dir else rel)
    return sorted(files)
