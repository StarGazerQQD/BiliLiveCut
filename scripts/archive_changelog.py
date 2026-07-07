"""CHANGELOG 归档脚本 (V0.1.14.3)。

动态扫描所有版本标题, 提取 0.1.X 系列号, 按 X 倒序自动选择最近三个系列保留在主 CHANGELOG.md 中,
其余按系列归档到 docs/changelog/CHANGELOG_PRE_0.1.X.md。
"""

from __future__ import annotations

import os
import re


def main() -> None:
    """执行 CHANGELOG 归档。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    changelog_path = os.path.join(root, "CHANGELOG.md")
    docs_dir = os.path.join(root, "docs", "changelog")
    os.makedirs(docs_dir, exist_ok=True)

    with open(changelog_path, encoding="utf-8") as f:
        content = f.read()
        lines = content.split("\n")

    # 1. 定位所有版本标题行
    versions: list[tuple[int, str, int]] = []  # [(line_idx, full_version, series_num)]
    for i, line in enumerate(lines):
        m = re.match(r"^## (V?0\.1\.(\d+)(?:\.\d+)?) ", line)
        if m:
            versions.append((i, m.group(1), int(m.group(2))))

    if not versions:
        print("ERROR: 未找到版本标题")
        return

    # 2. 动态提取所有三级版本系列, 按 X 倒序
    all_series = sorted({v[2] for v in versions}, reverse=True)
    print(f"发现三级版本系列: {all_series}")

    if len(all_series) <= 3:
        print(f"只有 {len(all_series)} 个系列, 无需归档。")
        return

    # 最近 3 个系列保留在主文件
    keep_series = set(all_series[:3])
    archive_series = all_series[3:]
    print(f"保留 (主 CHANGELOG): {sorted(keep_series, reverse=True)}")
    print(f"归档: {sorted(archive_series, reverse=True)}")

    # 3. 找到保留系列中最后一个版本标题的行号
    last_keep_line = max(i for i, _, sn in versions if sn in keep_series)

    # 4. 找到第一个需要归档的版本标题行 (归档起始)
    archive_start = None
    for line_idx, _, sn in versions:
        if sn in archive_series:
            archive_start = line_idx
            break

    # 归档行应该在最后一个保留行和第一个归档行之间
    # 从 last_keep_line 下一个边界开始切分
    # 找到最后一个属于保留系列的版本块结束位置
    current_idx = last_keep_line + 1
    while current_idx < len(lines) and lines[current_idx].strip().startswith("##"):
        # 跳过可能存在的分割线
        current_idx += 1

    # 向上搜索: 从 archive_start 向上找到 --- 分割线
    cut_line = archive_start if archive_start else len(lines)
    # 在 cut_line 之前寻找 --- 分割线作为切点
    for i in range(cut_line - 1, last_keep_line, -1):
        if lines[i].strip() == "---":
            cut_line = i
            break

    kept_lines = lines[:cut_line]
    archived_lines = lines[cut_line:]

    # 5. 按三级版本号分组归档
    current_series: int | None = None
    current_block: list[str] = []
    archive_files: dict[int, list[str]] = {}

    for line in archived_lines:
        m = re.match(r"^## (V?0\.1\.(\d+)(?:\.\d+)?) ", line)
        if m:
            series_num = int(m.group(2))
            if series_num not in archive_series:
                # 不应该出现, 但安全跳过
                continue
            if current_series is not None and current_block:
                archive_files.setdefault(current_series, []).extend(current_block)
            current_series = series_num
            current_block = [line]
        else:
            current_block.append(line)

    if current_series is not None and current_block:
        archive_files.setdefault(current_series, []).extend(current_block)

    # 6. 写入归档文件
    for series_num in sorted(archive_files.keys(), reverse=True):
        filename = f"CHANGELOG_PRE_0.1.{series_num}.md"
        filepath = os.path.join(docs_dir, filename)
        block = archive_files[series_num]
        content_trimmed = "\n".join(block).rstrip() + "\n"

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# CHANGELOG — 0.1.{series_num} 系列\n\n")
            f.write("> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。\n\n")
            f.write(content_trimmed)

        file_versions = []
        for line in block:
            m = re.match(r"^## (V?0\.1\.\d+(?:\.\d+)?)", line)
            if m:
                file_versions.append(m.group(1))
        print(f"  写入: {filename} ({len(file_versions)} 个版本)")

    # 7. 更新主 CHANGELOG
    archive_notice = (
        "\n\n---\n\n"
        "历史版本归档见 [docs/changelog/CHANGELOG_INDEX.md](docs/changelog/CHANGELOG_INDEX.md)。\n"
    )
    kept_content = "\n".join(kept_lines).rstrip() + archive_notice + "\n"

    with open(changelog_path, "w", encoding="utf-8") as f:
        f.write(kept_content)

    # 8. 生成 CHANGELOG_INDEX.md
    index_lines = [
        "# CHANGELOG 归档索引",
        "",
        "| 三级版本系列 | 文件 | 含版本 |",
        "|-------------|------|--------|",
    ]
    # 保留系列
    for sn in sorted(keep_series, reverse=True):
        label = "当前版本" if sn == max(keep_series) else f"0.1.{sn} 全系列"
        index_lines.append(f"| 0.1.{sn} | `../../CHANGELOG.md` | {label} |")

    # 归档系列
    for sn in sorted(archive_series, reverse=True):
        ver_names = []
        for _, fv, _sn in versions:
            if _sn == sn and fv not in ver_names:
                ver_names.append(fv)
        ver_list = ", ".join(ver_names)
        index_lines.append(f"| 0.1.{sn} | `CHANGELOG_PRE_0.1.{sn}.md` | {ver_list} |")

    index_content = "\n".join(index_lines) + "\n"
    index_path = os.path.join(docs_dir, "CHANGELOG_INDEX.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_content)

    print(f"\n  更新: CHANGELOG.md (保留 {len(keep_series)} 个系列)")
    print("  创建: CHANGELOG_INDEX.md")
    print("  完成!")


if __name__ == "__main__":
    main()
