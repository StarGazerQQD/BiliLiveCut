"""Portable Release 元数据 — 由 Payload 构建时写入，运行时读取。

业务代码通过此文件读取 RELEASE_VERSION 和 SOURCE_COMMIT，
避免对 README/CHANGELOG 等历史文档执行宽泛正则替换。
"""

from __future__ import annotations

RELEASE_VERSION: str = "0.1.17.1-alpha"
SOURCE_COMMIT: str = "2ebd509e8fa93cc09067888dbcf6d0984b976a16"
SOURCE_COMMIT_SHORT: str = "2ebd509"
BUILDER_COMMIT: str = ""
