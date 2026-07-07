"""命令行入口 — 调度器。

所有命令实现已迁移到 app/commands/ 子模块, 本文件仅负责:
- 创建 Typer 主应用
- 从子模块导入并注册命令
- 全局回调 (日志初始化)
- 顶层异常处理
"""

from __future__ import annotations

import typer

from app import __version__
from app.core.logging import setup_logging

app = typer.Typer(help="BiliLiveCut —— AI 直播实时切片系统 CLI")


@app.callback()
def _bootstrap() -> None:
    """所有命令执行前的初始化: 配置日志。"""
    setup_logging()


# ── 从子模块注册所有命令 ──────────────────────────────────
from app.commands import ALL_COMMANDS  # noqa: E402

for cmd_name, cmd_func, _help in ALL_COMMANDS:
    # Register each command on the main app
    help_text = _help if _help else cmd_func.__doc__
    app.command(name=cmd_name, help=help_text)(cmd_func)


# ── 版本命令 ─────────────────────────────────────────────
@app.command()
def version() -> None:
    """显示当前版本号。"""
    print(f"BiliLiveCut {__version__}")
