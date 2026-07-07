"""CLI 子命令 — 数据库维护。"""

from __future__ import annotations

import typer
from rich.console import Console

from app.core.config import settings

console = Console()


def cmd_db_reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认提示"),
) -> None:
    """重置数据库 (仅供开发使用)。

    删除当前数据库并重新创建。默认会先生成备份。

    :param yes: 跳过确认提示。
    """
    if not yes:
        console.print(f"[red]警告: 这将删除当前数据库并重新创建![/red]\n数据库路径: {settings.database_url}\n")
        from app.db.schema import _db_path

        console.print(f"绝对路径: {_db_path()}\n")
        confirm = typer.confirm("确认重置数据库?")
        if not confirm:
            console.print("[yellow]已取消。[/yellow]")
            raise typer.Exit()

    from app.db.migrate import reset_db

    ok = reset_db(yes=yes)
    if ok:
        console.print("[green]数据库已重置重建。[/green]")
    else:
        console.print("[red]数据库重置失败, 请检查日志。[/red]")
        raise typer.Exit(code=1)


def cmd_trends_purge(
    days: int = typer.Option(None, help="保留天数(留空用配置 TREND_RETENTION_DAYS)"),
) -> None:
    """清理超过保留期的网感资料库条目。

    :param days: 保留天数。
    """
    from app.trends.store import purge_old

    n = purge_old(days if days is not None else settings.trend_retention_days)
    console.print(f"[green]已清理 {n} 条过期条目。[/green]")


# 注册列表
DATABASE_COMMANDS = [
    ("db-reset", cmd_db_reset, None),
    ("trends-purge", cmd_trends_purge, None),
]
