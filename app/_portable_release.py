"""Portable Release 元数据 — 由 Payload 构建时写入，运行时读取。

业务代码通过此文件读取 RELEASE_VERSION 和 SOURCE_COMMIT，
避免对 README/CHANGELOG 等历史文档执行宽泛正则替换。
"""

from __future__ import annotations

RELEASE_VERSION: str = "0.1.17-alpha"
SOURCE_COMMIT: str = "c8c5e50eacabf58d8e61a1bd1b4de7971ae7475c"
SOURCE_COMMIT_SHORT: str = "c8c5e50"
BUILDER_COMMIT: str = ""
