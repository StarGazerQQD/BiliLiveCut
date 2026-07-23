"""Portable Release 元数据 — 由 Payload 构建时写入，运行时读取。

业务代码通过此文件读取 RELEASE_VERSION 和 SOURCE_COMMIT，
避免对 README/CHANGELOG 等历史文档执行宽泛正则替换。
"""

from __future__ import annotations

RELEASE_VERSION: str = "0.1.15.2-alpha"
SOURCE_COMMIT: str = "f2c291df2409bdf83dbf8f8a30d6b3ee1d44e8e0"
SOURCE_COMMIT_SHORT: str = "f2c291d"
BUILDER_COMMIT: str = ""
