"""CLI 子命令 — 数据库维护。"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from app.core.config import settings

console = Console()


@click.option("--yes", "-y", default=False, is_flag=True, help="跳过确认提示", show_default=True)
def cmd_db_reset(
    yes: bool = False,
) -> None:
    """重置数据库 (仅供开发使用)。

    删除当前数据库并重新创建。默认会先生成备份。

    :param yes: 跳过确认提示。
    """
    if not yes:
        console.print(f"[red]警告: 这将删除当前数据库并重新创建![/red]\n数据库路径: {settings.database_url}\n")
        from app.db.schema import _db_path

        console.print(f"绝对路径: {_db_path()}\n")
        confirm = click.confirm("确认重置数据库?")
        if not confirm:
            console.print("[yellow]已取消。[/yellow]")
            raise click.exceptions.Exit()

    from app.db.schema import reset_database

    ok = reset_database(yes=yes, backup=True)
    if ok:
        console.print("[green]数据库已重置重建。[/green]")
    else:
        console.print("[red]数据库重置失败, 请检查日志。[/red]")
        raise click.exceptions.Exit(code=1)


@click.option("--days", default=None, type=int, help="保留天数(留空用配置 TREND_RETENTION_DAYS)", show_default=True)
def cmd_trends_purge(
    days: int | None = None,
) -> None:
    """清理超过保留期的网感资料库条目。

    :param days: 保留天数。
    """
    from app.trends.store import purge_old

    n = purge_old(days if days is not None else settings.trend_retention_days)
    console.print(f"[green]已清理 {n} 条过期条目。[/green]")


@click.option("--database", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--storage", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
@click.option(
    "--map-dir",
    "directory_maps",
    type=(str, click.Path(exists=True, file_okay=False, path_type=Path)),
    multiple=True,
    help="外置目录的原路径和副本路径，可重复。",
)
def cmd_storage_check(database: Path, storage: Path, directory_maps: tuple[tuple[str, Path], ...]) -> None:
    """只读核对数据库副本与 storage 中的场次身份、分段登记和历史弹幕时间。"""
    from app.recording.storage_check import check_storage

    count, findings = check_storage(database, storage, directory_maps)
    click.echo(f"已核对 {count} 个数据库场次，待核对项 {len(findings)}")
    for finding in findings:
        click.echo(finding)
    if findings:
        raise click.exceptions.Exit(1)


@click.option("--output", type=click.Path(file_okay=False, path_type=Path), required=True, help="尚不存在的新目录。")
@click.option(
    "--source",
    multiple=True,
    type=(
        click.Path(exists=True, dir_okay=False, path_type=Path),
        click.Path(exists=True, file_okay=False, path_type=Path),
    ),
    help="离线数据库文件和对应 storage 目录，可重复。",
)
@click.option(
    "--files",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="仅从 storage 下剩余的场次文件重建，可重复。",
)
@click.option(
    "--manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="包含多个来源及外置目录的 JSON 清单。",
)
def cmd_storage_rebuild(
    output: Path, source: tuple[tuple[Path, Path], ...], files: tuple[Path, ...], manifest: Path | None
) -> None:
    """引用离线数据库或场次原文件，生成独立的媒体库。"""
    from app.recording.library_rebuild import LibraryRebuild, LibrarySource, rebuild_library

    if manifest and (source or files):
        raise click.UsageError("--manifest 与 --source、--files 请分开使用")
    if manifest:
        request = LibraryRebuild.model_validate_json(manifest.read_bytes())
    else:
        if not source and not files:
            raise click.UsageError("请提供 --source、--files 或 --manifest")
        request = LibraryRebuild(
            sources=[
                *(LibrarySource(database=db, storage=root) for db, root in source),
                *(LibrarySource(storage=root) for root in files),
            ]
        )
    click.echo(rebuild_library(request, output).model_dump_json(indent=2))


# 注册列表
DATABASE_COMMANDS = [
    ("db-reset", cmd_db_reset, None),
    ("storage-check", cmd_storage_check, None),
    ("storage-rebuild", cmd_storage_rebuild, None),
    ("trends-purge", cmd_trends_purge, None),
]
