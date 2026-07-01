"""独立的数据库初始化脚本。

等价于 ``python -m app.cli init``,方便在部署/CI 中直接调用。

用法::

    python scripts/init_db.py
"""

from __future__ import annotations

from app.core.logging import setup_logging
from app.db.session import init_db


def main() -> None:
    """初始化数据库并打印结果。"""
    setup_logging()
    init_db()
    print("数据库初始化完成。")


if __name__ == "__main__":
    main()
