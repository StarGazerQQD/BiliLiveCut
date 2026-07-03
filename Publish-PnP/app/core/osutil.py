"""操作系统相关的小工具。

目前提供跨平台"在文件管理器中打开目录"的能力,用于上传模块关闭时在直播结束后
弹出切片所在目录。所有操作都吞掉异常(无图形环境/无权限时不应导致流程失败)。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from loguru import logger


def open_path(path: str | Path) -> bool:
    """在系统文件管理器中打开一个目录或文件所在位置。

    * Windows:``os.startfile``;
    * macOS:``open``;
    * Linux:``xdg-open``。

    :param path: 目录或文件路径。
    :returns: 成功发起打开返回 ``True``;失败返回 ``False``(不抛异常)。
    """
    p = Path(path)
    target = str(p if p.exists() else p.parent)
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606 - 仅打开本机目录
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        logger.info("已请求打开目录: {}", target)
        return True
    except Exception as exc:  # noqa: BLE001 — 打开目录失败不应影响主流程
        logger.warning("打开目录失败 {}: {}", target, exc)
        return False
