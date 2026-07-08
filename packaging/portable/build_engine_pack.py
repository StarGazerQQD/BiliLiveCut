"""Engine Pack 构建器 — 薄入口，正式逻辑在 src/blc_portable/engine_pack/builder.py。"""
from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC_DIR))

from blc_portable.engine_pack.builder import main

if __name__ == "__main__":
    raise SystemExit(main())
