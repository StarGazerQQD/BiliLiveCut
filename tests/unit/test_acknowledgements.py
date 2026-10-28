from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_acknowledgements import check_acknowledgements


def _readme(body: str) -> str:
    return f"# 项目\n\n## 鸣谢\n\n{body}\n\n## 许可证\n\nCopyright StarGazerQQD\n"


def test_reference_links_and_other_platforms_preserve_existing_acknowledgements() -> None:
    markdown = _readme(
        "- GitHub: [Contributor][person]\n"
        "- Bilibili: [用户](https://space.bilibili.com/123)\n"
        "- [问题](https://github.com/StarGazerQQD/BiliLiveCut/issues/1)\n"
        "\n[person]: https://github.com/Contributor\n"
    )
    assert check_acknowledgements(markdown) == []


@pytest.mark.parametrize("identity", ["StarGazerQQD", "STARGAZERQQD", "stargazerqqd"])
def test_excluded_account_is_rejected_case_insensitively(identity: str) -> None:
    errors = check_acknowledgements(_readme(f"[{identity}](https://github.com/{identity})"))
    assert any("明确退出" in error for error in errors)


def test_renamed_excluded_account_still_fails() -> None:
    errors = check_acknowledgements(_readme("[维护者](https://github.com/StarGazerQQD)"))
    assert any("明确退出" in error for error in errors)
    assert any("ID 与主页不一致" in error for error in errors)


def test_duplicate_profile_ignores_case_and_trailing_slash() -> None:
    errors = check_acknowledgements(_readme("[Person](https://github.com/Person) [person](https://github.com/person/)"))
    assert any("账号重复" in error for error in errors)


@pytest.mark.parametrize(
    "markdown",
    ["## 许可证\n", "## 鸣谢\n## 鸣谢\n## 许可证", "## 鸣谢\n## 其他\n## 许可证", "## 许可证\n## 鸣谢"],
)
def test_missing_duplicate_or_misplaced_section_is_rejected(markdown: str) -> None:
    assert check_acknowledgements(markdown)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/Person",
        "https://github.com/Person?tab=repositories",
        "https://github.com/Person#about",
        "https://github.com:invalid/Person",
        "https://github.com:65536/Person",
    ],
)
def test_profile_link_requires_canonical_https_identity(url: str) -> None:
    assert check_acknowledgements(_readme(f"[Person]({url})"))


def test_fenced_examples_do_not_become_sections_or_acknowledgements() -> None:
    markdown = "```markdown\n## 鸣谢\n[StarGazerQQD](https://github.com/StarGazerQQD)\n```\n" + _readme(
        "[Person](https://github.com/Person)"
    )
    assert check_acknowledgements(markdown) == []


def test_checker_cli_is_read_only_and_rejects_excluded_account(tmp_path: Path) -> None:
    path = tmp_path / "README.md"
    text = _readme("[StarGazerQQD](https://github.com/StarGazerQQD)")
    path.write_text(text, encoding="utf-8")
    script = Path(__file__).resolve().parents[2] / "scripts/check_acknowledgements.py"
    result = subprocess.run([sys.executable, str(script), "--readme", str(path)], capture_output=True, check=False)
    assert result.returncode == 1
    assert path.read_text(encoding="utf-8") == text
