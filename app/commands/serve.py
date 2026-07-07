"""CLI 子命令 — Web 服务。"""

from __future__ import annotations

import typer
from rich.console import Console

console = Console()


def cmd_serve(
    host: str = typer.Option("127.0.0.1", help="监听地址"),
    port: int = typer.Option(8000, help="监听端口"),
    reload: bool = typer.Option(False, "--reload", help="开发热重载"),
) -> None:
    """启动 Web 管理后台(阶段4,需安装 .[web])。

    :param host: 监听地址。
    :param port: 监听端口。
    :param reload: 是否开启热重载。
    """
    try:
        import uvicorn
    except ImportError as exc:
        console.print('[red]未安装 Web 依赖。请执行: pip install -e ".[web]"[/red]')
        raise typer.Exit(code=1) from exc

    # P0: non-loopback requires password
    from app.core.config import settings as _srv_cfg

    if not _srv_cfg.admin_password:
        from ipaddress import ip_address

        try:
            addr = ip_address(host.replace("localhost", "127.0.0.1"))
            if not addr.is_loopback:
                console.print(
                    "[red]拒绝启动 Web 管理后台：\n"
                    "当前监听地址不是本地地址，但 ADMIN_PASSWORD 为空。\n"
                    "请设置管理员密码或改为 127.0.0.1。[/red]"
                )
                raise typer.Exit(code=1)
        except ValueError:
            # non-IP hostname (like docker), also require password
            console.print(
                "[red]拒绝启动 Web 管理后台：\n"
                "当前监听地址不是本地地址，但 ADMIN_PASSWORD 为空。\n"
                "请设置管理员密码或改为 127.0.0.1。[/red]"
            )
            raise typer.Exit(code=1) from None

    console.print(f"[green]控制台启动中[/green] -> http://{host}:{port}")
    uvicorn.run("app.web.main:app", host=host, port=port, reload=reload)


# 注册列表
SERVE_COMMANDS = [
    ("serve", cmd_serve, None),
]
