"""Portable 命令行入口的控制台兼容性工具。"""

from __future__ import annotations

import sys


def configure_console_encoding() -> None:
    """将当前进程输出切换为 UTF-8，避免旧版 Windows 代码页无法输出中文。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, TypeError, ValueError):
            # 测试捕获器或嵌入式宿主可能不允许修改流配置；此时保留宿主设置。
            continue
