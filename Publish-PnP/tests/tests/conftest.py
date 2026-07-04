"""pytest 公共夹具。

提供一个隔离的、基于临时目录的 SQLite 数据库,避免污染真实 storage。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


@pytest.fixture()
def temp_db(tmp_path, monkeypatch: MonkeyPatch) -> Iterator[None]:
    """创建一个临时 SQLite 数据库并重建引擎。

    通过环境变量覆盖 ``DATABASE_URL`` 与 ``STORAGE_ROOT``,清空配置缓存后
    重新导入会话模块,确保引擎指向临时库。

    :param tmp_path: pytest 提供的临时目录。
    :param monkeypatch: 用于设置环境变量。
    :yields: 无返回值,仅在测试期间提供隔离环境。
    """
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))

    # 清空配置单例缓存,使新环境变量生效。
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    config_module.settings = config_module.get_settings()

    # 重建数据库引擎以指向临时库。
    import importlib

    from app.db import session as session_module

    importlib.reload(session_module)
    session_module.init_db()

    yield
