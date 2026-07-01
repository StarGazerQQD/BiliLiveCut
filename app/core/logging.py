"""日志系统。

基于 loguru 提供结构化、带轮转的日志:

* 控制台彩色输出(便于开发);
* 文件输出到 ``storage/logs/blc.log``,自动轮转与压缩(便于排错追溯);
* 通过 :func:`setup_logging` 在程序入口初始化一次。

业务模块直接 ``from loguru import logger`` 使用即可。
"""

from __future__ import annotations

import sys

from loguru import logger

from app.core.config import settings
from app.core.paths import logs_dir

_CONFIGURED = False


def setup_logging() -> None:
    """初始化全局日志处理器(幂等)。

    多次调用只会生效一次,避免重复添加 handler 导致日志重复输出。
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    # 移除 loguru 默认 handler,改用自定义配置。
    logger.remove()

    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    # 控制台:彩色,级别取自配置。
    logger.add(sys.stderr, level=settings.log_level, format=fmt, enqueue=True)

    # 文件:按大小轮转 10MB,保留 14 天,压缩为 zip,便于长期追溯。
    logger.add(
        logs_dir() / "blc.log",
        level="DEBUG",
        format=fmt,
        rotation="10 MB",
        retention="14 days",
        compression="zip",
        encoding="utf-8",
        enqueue=True,  # 多进程/多线程安全
        backtrace=True,
        diagnose=settings.app_env == "dev",  # 生产环境不输出变量值,避免泄露
    )

    # 数据库 sink:把 WARNING 及以上写入 system_logs,供 Web 后台查看。
    # enqueue=True 让写库发生在独立线程,不阻塞业务;内部已吞掉自身异常,避免递归。
    logger.add(_db_sink, level="WARNING", enqueue=True)

    _CONFIGURED = True
    logger.debug("日志系统已初始化(env={}, level={})", settings.app_env, settings.log_level)


def _db_sink(message: object) -> None:
    """loguru sink:把一条日志记录写入 ``system_logs`` 表。

    任何写库异常都被静默忽略,避免日志系统反过来拖垮业务或造成递归报错。

    :param message: loguru 传入的消息对象(含 ``.record``)。
    """
    try:
        record = message.record  # type: ignore[attr-defined]
        # 延迟导入,规避循环依赖(db -> logging)。
        from app.db.models import SystemLog
        from app.db.session import get_session

        with get_session() as db:
            db.add(
                SystemLog(
                    level=record["level"].name,
                    module=record["name"],
                    event=record["function"],
                    message=record["message"],
                )
            )
    except Exception:  # noqa: BLE001 — 日志 sink 绝不能抛出
        pass
