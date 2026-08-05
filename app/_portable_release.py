"""Portable Release 元数据 — 由 Payload 构建时写入，运行时读取。

业务代码通过此文件读取 RELEASE_VERSION 和 SOURCE_COMMIT，
避免对 README/CHANGELOG 等历史文档执行宽泛正则替换。
"""

from __future__ import annotations

RELEASE_VERSION: str = "0.1.17-alpha"
SOURCE_COMMIT: str = "bab1bf6d4c04d7d74463a2dcdd9b78cd1586ae99"
SOURCE_COMMIT_SHORT: str = "bab1bf6"
BUILDER_COMMIT: str = ""
