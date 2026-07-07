"""CLI 子命令 — 配置管理。"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

console = Console()


def cmd_set_upload(
    biliup: bool = typer.Option(None, "--biliup/--no-biliup", help="启用/关闭 Biliup 上传"),
    auto: bool = typer.Option(None, "--auto/--no-auto", help="成品就绪后自动上传"),
) -> None:
    """查看或设置上传开关(与 Web 后台共享同一持久化状态)。

    :param biliup: 是否启用 biliup 上传。
    :param auto: 是否自动上传。
    """
    from app.core import settings_store

    if biliup is not None:
        settings_store.set_bool("biliup_enabled", biliup)
    if auto is not None:
        settings_store.set_bool("auto_upload", auto)
    console.print(
        f"biliup_enabled={settings_store.biliup_enabled()} "
        f"auto_upload={settings_store.auto_upload_enabled()} "
        f"upload_active={settings_store.upload_active()}"
    )


def cmd_llm_list() -> None:
    """列出已配置的大模型(按优先级),显示是否可用(key 掩码)。"""
    from app.analysis import llm_providers as provs

    items = provs.load_providers()
    if not items:
        console.print("[yellow]未配置任何大模型。可在 Web「模型」页添加,或配置 .env[/yellow]")
        return
    table = Table(title="大模型(按优先级升序)")
    for col in ("priority", "name", "model", "base_url", "key", "enabled"):
        table.add_column(col)
    for p in items:
        hint = f"****{p.api_key[-4:]}" if p.api_key else "(未配置)"
        table.add_row(
            str(p.priority),
            p.name,
            p.model,
            p.base_url,
            hint,
            "是" if p.enabled else "否",
        )
    console.print(table)


def cmd_llm_test() -> None:
    """逐个测试已启用大模型的连通性(各发一次极小请求)。"""
    from app.analysis import llm as llm_mod
    from app.analysis import llm_providers as provs

    providers = provs.active_providers()
    if not providers:
        console.print("[yellow]无可用大模型(需已启用且配置 key)。[/yellow]")
        raise typer.Exit(code=1)
    for p in providers:
        try:
            text = llm_mod._complete(p, "ping", max_tokens=1)
            console.print(f"[green]OK[/green] {p.name}({p.model}) -> {(text or '')[:40]!r}")
        except Exception as exc:  # noqa: BLE001 — 汇总每个 provider 的错误
            console.print(f"[red]FAIL[/red] {p.name}({p.model}): {str(exc)[:160]}")


# 注册列表
CONFIG_COMMANDS = [
    ("set-upload", cmd_set_upload, None),
    ("llm-list", cmd_llm_list, None),
    ("llm-test", cmd_llm_test, None),
]
