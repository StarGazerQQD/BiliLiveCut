"""Portable Release 元数据 — 由 Payload 构建时写入，运行时读取。

业务代码通过此文件读取 RELEASE_VERSION 和 SOURCE_COMMIT，
避免对 README/CHANGELOG 等历史文档执行宽泛正则替换。
"""

from __future__ import annotations

RELEASE_VERSION: str = "0.1.15.2-alpha"
SOURCE_COMMIT: str = "1b47a0942b04efc1c11b11e1f74bc970f843f4c4"
SOURCE_COMMIT_SHORT: str = "1b47a09"
BUILDER_COMMIT: str = ""
