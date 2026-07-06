"""数据库管理命令 (V0.1.12.9)。

V0.1.12.9: 迁移框架已移除。本模块仅保留 db reset CLI 命令。
所有 Schema 管理与校验逻辑已迁移至 app.db.schema。
"""

from __future__ import annotations

from app.db.schema import reset_database as _reset_database


def reset_db(*, yes: bool = False) -> bool:
    """重置数据库 (删除 + 重建, 仅供开发/CI 使用)。

    安全措施:
    - 显示数据库绝对路径
    - 要求确认 (除非 --yes)
    - 默认生成备份

    :param yes: 跳过确认提示
    :returns: True 表示成功
    """
    return _reset_database(yes=yes, backup=True)
