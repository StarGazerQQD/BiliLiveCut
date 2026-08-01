"""Portable Release 元数据 — 由 Payload 构建时写入，运行时读取。

业务代码通过此文件读取 RELEASE_VERSION 和 SOURCE_COMMIT，
避免对 README/CHANGELOG 等历史文档执行宽泛正则替换。
"""

from __future__ import annotations

RELEASE_VERSION: str = "0.1.16.3-alpha"
SOURCE_COMMIT: str = "daa0296ca577b124f0d6e8c9eab2041b27bd7f3b"
SOURCE_COMMIT_SHORT: str = "daa0296"
BUILDER_COMMIT: str = ""
