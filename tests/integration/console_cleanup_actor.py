# ruff: noqa: D100
from __future__ import annotations

import sys
from pathlib import Path

from app.analysis.transcription.process_tree import ProcessTree
from app.cli import main


def run() -> None:
    """执行真实 console，仅在所属树清理的 OS 边界注入确认失败。"""
    counter = Path(sys.argv.pop(1))
    with counter.open("a", encoding="utf-8") as output:
        output.write("1")
    original = ProcessTree.terminate

    def unconfirmed(tree: ProcessTree) -> None:
        original(tree)
        raise OSError("controlled cleanup confirmation failure")

    ProcessTree.terminate = unconfirmed
    main()


if __name__ == "__main__":
    run()
