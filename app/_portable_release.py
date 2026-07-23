"""Portable Release 元数据 — 由 Payload 构建时写入，运行时读取。

业务代码通过此文件读取 RELEASE_VERSION 和 SOURCE_COMMIT，
避免对 README/CHANGELOG 等历史文档执行宽泛正则替换。
"""

from __future__ import annotations

RELEASE_VERSION: str = "0.1.15.2-alpha"
SOURCE_COMMIT: str = "0fe24a5f050c7110b2214570ac165d828f5f363c"
SOURCE_COMMIT_SHORT: str = "0fe24a5"
BUILDER_COMMIT: str = ""
