"""命令行入口 — 调度器。

所有命令实现已迁移到 app/commands/ 子模块, 本文件仅负责:
- 创建 Click 主应用
- 从子模块导入并注册命令
- 全局回调 (日志初始化)
- 顶层异常处理
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import click
from loguru import logger

from app import __version__
from app.core.logging import setup_logging
from app.core.sanitize import safe_exception_summary

P = ParamSpec("P")
R = TypeVar("R")


def _diagnostic_command(command: Callable[P, R]) -> Callable[P, R]:
    """统一命令异常出口，保留安全日志并避免输出凭据与局部变量。"""

    @wraps(command)
    def invoke(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return command(*args, **kwargs)
        except (click.ClickException, click.exceptions.Exit, click.Abort):
            raise
        except Exception as exc:  # noqa: BLE001 — CLI 最外层错误边界
            context = click.get_current_context(silent=True)
            logger.opt(exception=exc).error(
                "命令失败 command={}", context.command.name if context else command.__name__
            )
            click.echo(safe_exception_summary(exc), err=True)
            raise click.exceptions.Exit(code=1) from None

    return invoke


class BLCCommand(click.Command):
    """参数解析成功后才准备数据库，帮助和参数错误不触碰业务数据。"""

    @_diagnostic_command
    def invoke(self, ctx: click.Context) -> object:
        """为业务命令准备当前数据库，再调用实际实现。"""
        if self.name not in {"version", "doctor", "serve", "console", "backend", "frontend", "start", "init"}:
            from app.db.session import init_db

            init_db()
        return super().invoke(ctx)


class BLCGroup(click.Group):
    """让后续注册的命令共享初始化与安全错误处理。"""

    command_class = BLCCommand


@click.group(cls=BLCGroup, context_settings={"help_option_names": ["-h", "--help"]})
@_diagnostic_command
def app() -> None:
    """BiliLiveCut —— AI 直播实时切片系统 CLI。"""
    setup_logging()


# ── 从子模块注册所有命令 ──────────────────────────────────
from app.commands import ALL_COMMANDS  # noqa: E402

for cmd_name, cmd_func, _help in ALL_COMMANDS:
    # Register each command on the main app
    # Python 参数文档不属于终端帮助，参数由 Click 单独列出。
    help_text = _help or (cmd_func.__doc__ or "").split(":param", 1)[0].rstrip()
    # 同一实现可注册兼容别名；显式复制参数，避免 command() 消耗装饰器元数据。
    app.add_command(
        BLCCommand(
            name=cmd_name,
            callback=cmd_func,
            params=list(reversed(getattr(cmd_func, "__click_params__", []))),
            help=help_text,
        )
    )


# ── 版本命令 ─────────────────────────────────────────────
@app.command()
def version() -> None:
    """显示当前版本号。"""
    print(f"BiliLiveCut {__version__}")


def main() -> None:
    """初始化 UTF-8 输出后运行 CLI，覆盖帮助和参数错误等回调前输出。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, TypeError, ValueError):
            # 测试捕获器或嵌入式宿主可能禁止修改流配置；保留宿主设置。
            continue
    from app.core.supervisor import standalone_process

    with standalone_process():
        app()


if __name__ == "__main__":
    main()
