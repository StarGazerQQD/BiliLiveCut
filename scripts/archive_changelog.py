"""V0.1.14: CHANGELOG 归档脚本。

将主 CHANGELOG.md 中超过最近 3 个三级版本系列 (0.1.13/12/11) 的内容
归档到 docs/changelog/CHANGELOG_PRE_0.1.X.md 文件。
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG_PATH = os.path.join(ROOT, "CHANGELOG.md")
DOCS_DIR = os.path.join(ROOT, "docs", "changelog")


def main() -> None:
    """执行 CHANGELOG 归档。"""
    os.makedirs(DOCS_DIR, exist_ok=True)

    with open(CHANGELOG_PATH, encoding="utf-8") as f:
        content = f.read()
        lines = content.split("\n")

    # 1. 定位所有版本标题行
    versions = []  # [(line_idx, full_version, series_num)]
    for i, line in enumerate(lines):
        m = re.match(r"^## (V0\.1\.(\d+)(?:\.\d+)?) Alpha", line)
        if m:
            versions.append((i, m.group(1), int(m.group(2))))

    if not versions:
        print("ERROR: 未找到版本标题")
        return

    # 2. 确定归档边界: 保留 0.1.13, 0.1.12, 0.1.11
    keep_series = {13, 12, 11}

    # 找到最后一个属于保留系列的版本行号
    last_keep_line = -1
    for line_idx, full_ver, series_num in versions:
        if series_num in keep_series:
            last_keep_line = line_idx

    if last_keep_line == -1:
        print("ERROR: 未找到保留系列版本")
        return

    # 找到 0.1.11 系列最后一个版本标题的下一行 (归档开始)
    # 实际上是 versions 中第一个不属于保留系列的行的起始
    archive_start_line = None
    for line_idx, full_ver, series_num in versions:
        if series_num not in keep_series:
            archive_start_line = line_idx
            break

    if archive_start_line is None:
        print("没有需要归档的版本")
        return

    # 3. 切分内容
    kept_lines = lines[:archive_start_line]
    archived_lines = lines[archive_start_line:]

    # 4. 按三级版本号分组归档
    current_series = None
    current_block: list[str] = []
    archive_files: dict[int, list[str]] = {}

    for line in archived_lines:
        m = re.match(r"^## (V0\.1\.(\d+)(?:\.\d+)?) Alpha", line)
        if m:
            series_num = int(m.group(2))
            # 保存上一个 series 的块
            if current_series is not None and current_block:
                archive_files.setdefault(current_series, []).extend(current_block)
            current_series = series_num
            current_block = [line]
        else:
            current_block.append(line)

    # 保存最后一个
    if current_series is not None and current_block:
        archive_files.setdefault(current_series, []).extend(current_block)

    # 5. 写入归档文件
    header_text = """# Changelog

"""
    archived_versions = []
    for series_num in sorted(archive_files.keys(), reverse=True):
        filename = f"CHANGELOG_PRE_0.1.{series_num}.md"
        filepath = os.path.join(DOCS_DIR, filename)
        block = archive_files[series_num]
        content_trimmed = "\n".join(block).rstrip() + "\n"

        # 添加归档头部
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# Changelog — 0.1.{series_num} 系列 (已归档)\n\n")
            f.write(
                f"> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。\n\n"
            )
            f.write(content_trimmed)

        # 记录此文件包含的版本
        file_versions = []
        for line in block:
            m = re.match(r"^## (V0\.1\.\d+(?:\.\d+)?) Alpha", line)
            if m:
                file_versions.append(m.group(1))
        archived_versions.append((filename, series_num, file_versions))
        print(f"  写入: {filename} ({len(file_versions)} 个版本)")

    # 6. 更新主 CHANGELOG
    archive_notice = (
        "\n\n---\n\n"
        "历史版本归档见 [docs/changelog/CHANGELOG_INDEX.md](docs/changelog/CHANGELOG_INDEX.md)。\n"
    )
    kept_content = "\n".join(kept_lines).rstrip() + archive_notice + "\n"

    with open(CHANGELOG_PATH, "w", encoding="utf-8") as f:
        f.write(kept_content)

    # 7. 生成 CHANGELOG_INDEX.md
    index_lines = [
        "# CHANGELOG 归档索引",
        "",
        "| 三级版本系列 | 文件 | 含版本 |",
        "|-------------|------|--------|",
        "| 0.1.13 | `../../CHANGELOG.md` | 当前版本 |",
        "| 0.1.12 | `../../CHANGELOG.md` | 0.1.12 全系列 |",
        "| 0.1.11 | `../../CHANGELOG.md` | 0.1.11 全系列 |",
    ]
    for filename, series_num, file_versions in archived_versions:
        ver_list = ", ".join(file_versions)
        index_lines.append(
            f"| 0.1.{series_num} | `{filename}` | {ver_list} |"
        )

    index_content = "\n".join(index_lines) + "\n"
    index_path = os.path.join(DOCS_DIR, "CHANGELOG_INDEX.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_content)

    print(f"\n  更新: CHANGELOG.md (保留 3 个系列)")
    print(f"  创建: CHANGELOG_INDEX.md")
    print("  完成!")


if __name__ == "__main__":
    main()
