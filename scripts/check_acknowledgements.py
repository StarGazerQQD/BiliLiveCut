# ruff: noqa: D100
from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

EXCLUDED_GITHUB_IDS = frozenset({"stargazerqqd"})
ROOT = Path(__file__).resolve().parent.parent


def check_acknowledgements(markdown: str) -> list[str]:
    """检查真实 Markdown 鸣谢区块，不修改名单或访问外部服务。"""
    tokens = MarkdownIt("commonmark").parse(markdown)
    headings = [
        (index, tokens[index + 1].content)
        for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.tag == "h2"
    ]
    acknowledgements = [index for index, (_, title) in enumerate(headings) if title == "鸣谢"]
    licenses = [index for index, (_, title) in enumerate(headings) if title == "许可证"]
    if len(acknowledgements) != 1 or len(licenses) != 1:
        return ["README 必须包含唯一的二级鸣谢与许可证区块"]
    position = acknowledgements[0]
    if licenses[0] != position + 1:
        return ["鸣谢区块必须紧邻并位于许可证之前"]
    errors: list[str] = []
    seen: set[str] = set()
    for token in tokens[headings[position][0] : headings[position + 1][0]]:
        children = token.children or []
        for index, child in enumerate(children):
            if child.type != "link_open":
                continue
            url = urlsplit(child.attrGet("href") or "")
            parts = url.path.strip("/").split("/")
            if url.hostname is None or url.hostname.casefold() != "github.com" or len(parts) != 1:
                continue
            identity = parts[0]
            if not identity:
                continue
            if (
                url.scheme != "https"
                or url.query
                or url.fragment
                or url.username
                or url.password
                or url.netloc.casefold() != "github.com"
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", identity)
            ):
                errors.append("GitHub 鸣谢主页必须使用无额外参数的 HTTPS 用户主页")
                continue
            label: list[str] = []
            for item in children[index + 1 :]:
                if item.type == "link_close":
                    break
                if item.type in {"text", "code_inline"}:
                    label.append(item.content)
            normalized = identity.casefold()
            if "".join(label).strip().lstrip("@").casefold() != normalized:
                errors.append(f"GitHub 鸣谢 ID 与主页不一致: {identity}")
            if normalized in EXCLUDED_GITHUB_IDS:
                errors.append(f"已明确退出的 GitHub 账号不能加入鸣谢: {identity}")
            if normalized in seen:
                errors.append(f"GitHub 鸣谢账号重复: {identity}")
            seen.add(normalized)
    return errors


def main() -> int:
    """读取指定 README 并返回检查状态，保留所有现有文件。"""
    parser = argparse.ArgumentParser(description="检查 README 鸣谢位置、GitHub ID 和明确退出名单")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    arguments = parser.parse_args()
    errors = check_acknowledgements(arguments.readme.read_text(encoding="utf-8"))
    for error in errors:
        print(f"[ERROR] {error}")
    if errors:
        return 1
    print("[OK] README 鸣谢区块、GitHub ID 与退出名单检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
