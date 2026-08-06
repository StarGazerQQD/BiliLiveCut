"""Portable Release 元数据 — 由 Payload 构建时写入，运行时读取。

业务代码通过此文件读取 RELEASE_VERSION 和 SOURCE_COMMIT，
避免对 README/CHANGELOG 等历史文档执行宽泛正则替换。
"""

from __future__ import annotations

RELEASE_VERSION: str = "0.1.17.1-alpha"
SOURCE_COMMIT: str = "2dc167da770a367f5d6e983941497f47c2918f35"
SOURCE_COMMIT_SHORT: str = "2dc167d"
BUILDER_COMMIT: str = ""
