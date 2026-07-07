"""版本一致性检查 — 确保所有位置的版本号一致。

唯一真源: app/__init__.py 的 __version__。

运行:
    python scripts/check_version_consistency.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
errors: list[str] = []


def _read_py_version() -> str:
    """从 app/__init__.py 读取版本。"""
    path_py = ROOT / "app" / "__init__.py"
    content = path_py.read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"(.+?)"', content, re.MULTILINE)
    if not m:
        errors.append("app/__init__.py: 找不到 __version__ 定义")
        sys.exit(1)
    return m.group(1)


def check_pyproject_toml(expected: str) -> None:
    """检查 pyproject.toml 的 version。"""
    path = ROOT / "pyproject.toml"
    content = path.read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"(.+?)"', content, re.MULTILINE)
    if not m:
        errors.append(f"{path.name}: 找不到 version 定义")
        return
    actual = m.group(1)
    if actual != expected:
        errors.append(f"{path.name}: version={actual!r}, 期望={expected!r}")
    else:
        print(f"  [OK] pyproject.toml: {actual}")


def check_readme(expected: str) -> None:
    """检查 README.md 的版本。"""
    path = ROOT / "README.md"
    content = path.read_text(encoding="utf-8")
    # 匹配 README 中的版本号: "V0.1.14.3 Alpha" 或 "V0.1.14.3-alpha"
    m = re.search(r"(V\d+\.\d+\.\d+(?:\.\d+)?(?:\s*Alpha)?)", content, re.IGNORECASE)
    if not m:
        errors.append(f"{path.name}: 找不到版本号")
        return
    actual = m.group(0)
    # 检查版本号是否匹配
    ver_match = re.search(r"V(\d+\.\d+\.\d+(?:\.\d+)?)", actual, re.IGNORECASE)
    if ver_match:
        base = ver_match.group(1)
        if expected.startswith(base):
            print(f"  [OK] README.md: {actual}")
            return
        errors.append(f"{path.name}: version={actual!r}, 不匹配期望={expected!r}")
    else:
        errors.append(f"{path.name}: 无法解析版本号: {actual!r}")


def check_changelog(expected: str) -> None:
    """检查 CHANGELOG.md 的最新版本。"""
    path = ROOT / "CHANGELOG.md"
    content = path.read_text(encoding="utf-8")
    m = re.search(
        r"##\s+V\d+\.\d+\.\d+(?:\.\d+)?(?:\s*Alpha)?\s*\(\d{4}-\d{2}-\d{2}\)",
        content,
    )
    if not m:
        errors.append(f"{path.name}: 找不到最新版本标题")
        return
    title = m.group(0)
    title_ver = re.search(r"V(\d+\.\d+\.\d+\.\d+)", title)
    base_ver = re.match(r"^(\d+\.\d+\.\d+\.\d+)", expected) or re.match(
        r"^(\d+\.\d+\.\d+)", expected
    )
    if not base_ver:
        errors.append(f"{path.name}: 无法解析期望版本 {expected!r}")
        return
    expected_prefix = base_ver.group(1)
    if title_ver and title_ver.group(1).startswith(expected_prefix):
        print(f"  [OK] CHANGELOG.md: {title.strip()}")
    elif expected_prefix in title:
        print(f"  [OK] CHANGELOG.md: {title.strip()}")
    else:
        errors.append(f"{path.name}: 最新标题={title.strip()!r}, 不匹配期望={expected!r}")


def check_runtime(expected: str) -> None:
    """检查运行时版本。"""
    sys.path.insert(0, str(ROOT))
    try:
        import app

        actual = app.__version__
        if actual == expected:
            print(f"  [OK] runtime: {actual}")
        else:
            errors.append(f"runtime: {actual!r}, 期望={expected!r}")
    except Exception as exc:
        errors.append(f"runtime: 导入失败: {exc}")


def main() -> int:
    """执行所有检查, 返回 0 表示一致。"""
    expected = _read_py_version()
    print(f"版本真源 (app/__init__.py): {expected}")
    print()

    check_pyproject_toml(expected)
    check_readme(expected)
    check_changelog(expected)
    check_runtime(expected)

    print()
    if errors:
        print(f"发现 {len(errors)} 处不一致:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("所有版本一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
