"""构建 Payload 的快捷入口。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging" / "portable" / "src"))

from blc_portable.payload.builder import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
