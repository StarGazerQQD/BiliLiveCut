"""Changelog 归档脚本回归测试。"""

from __future__ import annotations

from pathlib import Path

from scripts.archive_changelog import _write_index


def test_write_index_keeps_preexisting_archive_series(tmp_path: Path) -> None:
    """重建索引时不得丢失早于本轮归档的历史系列。"""
    (tmp_path / "CHANGELOG_PRE_0.1.11.md").write_text(
        "# CHANGELOG — 0.1.11 系列\n\n## V0.1.11 Alpha (2026-06-01)\n",
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG_PRE_0.1.13.md").write_text(
        "# CHANGELOG — 0.1.13 系列\n\n## V0.1.13.2 Alpha (2026-07-06)\n",
        encoding="utf-8",
    )

    _write_index(str(tmp_path), {14, 15, 16})

    index = (tmp_path / "CHANGELOG_INDEX.md").read_text(encoding="utf-8")
    assert "| 0.1.16 | `../../CHANGELOG.md` | 当前版本 |" in index
    assert "| 0.1.13 | `CHANGELOG_PRE_0.1.13.md` | V0.1.13.2 |" in index
    assert "| 0.1.11 | `CHANGELOG_PRE_0.1.11.md` | V0.1.11 |" in index
