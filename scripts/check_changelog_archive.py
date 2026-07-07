"""CHANGELOG 归档完整性校验。

验证:
1. 归档前版本标题集合 == 归档后主文件 + 归档文件版本标题集合
2. 每个系列只存在于一个文件
3. 最近三个系列位于主 CHANGELOG
4. 更早系列位于独立文件
5. 归档文件命名正确
6. 索引链接有效
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def extract_versions_from_file(filepath: str) -> list[str]:
    """提取文件中的所有 0.1.X 版本标题。"""
    if not os.path.isfile(filepath):
        return []
    with open(filepath, encoding="utf-8") as f:
        content = f.read()
    versions: list[str] = []
    for line in content.split("\n"):
        m = re.match(r"^## (V?0\.1\.\d+(?:\.\d+)?)", line)
        if m:
            versions.append(m.group(1).lstrip("Vv"))
    return versions


def extract_series_from_versions(versions: list[str]) -> set[int]:
    """从版本号列表中提取三级版本系列号。"""
    series: set[int] = set()
    for v in versions:
        m = re.match(r"0\.1\.(\d+)", v)
        if m:
            series.add(int(m.group(1)))
    return series


def main() -> int:
    """执行完整性校验。"""
    root = Path(__file__).parent.parent
    changelog_path = root / "CHANGELOG.md"
    docs_dir = root / "docs" / "changelog"
    index_path = docs_dir / "CHANGELOG_INDEX.md"

    all_errors: list[str] = []

    # 1. 收集所有源文件中的版本
    main_versions = extract_versions_from_file(str(changelog_path))
    main_series = extract_series_from_versions(main_versions)

    archive_versions: dict[int, list[str]] = {}
    for fn in sorted(os.listdir(docs_dir)):
        if re.match(r"CHANGELOG_PRE_0\.1\.\d+\.md", fn):
            fp = os.path.join(docs_dir, fn)
            versions = extract_versions_from_file(fp)
            m = re.match(r"CHANGELOG_PRE_0\.1\.(\d+)\.md", fn)
            if m:
                sn = int(m.group(1))
                if versions:
                    archive_versions[sn] = versions
                    print(f"  归档文件: {fn} -> 0.1.{sn} ({len(versions)} 个版本: {versions})")
                else:
                    all_errors.append(f"归档文件 {fn} 未包含版本标题")

    all_series = sorted(set(list(main_series) + list(archive_versions.keys())), reverse=True)
    print(f"\n全部三级版本系列: {all_series}")

    # 2. 验证最近三个系列在主文件
    if len(all_series) >= 3:
        latest_3 = set(all_series[:3])
        for sn in latest_3:
            if sn not in main_series:
                all_errors.append(f"系列 0.1.{sn} 不在主 CHANGELOG.md 中, 但在最近三个系列中")
        older = all_series[3:]
        for sn in older:
            if sn in main_series:
                all_errors.append(f"旧系列 0.1.{sn} 仍在主 CHANGELOG.md 中, 应归档")
    else:
        for sn in all_series:
            if sn not in main_series:
                all_errors.append(f"系列 0.1.{sn} 不在主 CHANGELOG.md 中")

    # 3. 验证每个系列只存在于一个文件
    for sn in set(main_series) & set(archive_versions.keys()):
        all_errors.append(f"系列 0.1.{sn} 同时存在于主 CHANGELOG.md 和归档文件中 (重复)")

    # 4. 验证归档文件命名
    for sn in archive_versions:
        expected_fn = f"CHANGELOG_PRE_0.1.{sn}.md"
        actual_fn = os.path.join(docs_dir, expected_fn)
        if not os.path.isfile(actual_fn):
            all_errors.append(f"归档文件 {expected_fn} 缺失")

    # 5. 验证索引文件存在和链接有效
    if not index_path.exists():
        all_errors.append("CHANGELOG_INDEX.md 缺失")
    else:
        with open(index_path, encoding="utf-8") as f:
            index_content = f.read()
        for sn in main_series:
            if f"0.1.{sn}" not in index_content:
                all_errors.append(f"索引中缺少 0.1.{sn} (在主 CHANGELOG) 的条目")
        for sn in archive_versions:
            if f"0.1.{sn}" not in index_content:
                all_errors.append(f"索引中缺少 0.1.{sn} (归档) 的条目")

        # 验证索引链接
        main_ref = "`../../CHANGELOG.md`"
        if main_ref not in index_content:
            all_errors.append("索引中缺少主 CHANGELOG.md 的链接")

    # 6. 输出结果
    print(f"\n主 CHANGELOG: {len(main_versions)} 个版本 -> 系列 {sorted(main_series)}")
    print(f"归档文件: {len(archive_versions)} 个")

    if all_errors:
        print(f"\n{'=' * 60}")
        print(f"发现 {len(all_errors)} 个问题:")
        for err in all_errors:
            print(f"  - {err}")
        print(f"{'=' * 60}")
        return 1
    else:
        print("\n所有校验通过!")
        return 0


if __name__ == "__main__":
    exit(main())
