"""Portable Release 元数据 — 由 Payload 构建时写入，运行时读取。

业务代码通过此文件读取 RELEASE_VERSION 和 SOURCE_COMMIT，
避免对 README/CHANGELOG 等历史文档执行宽泛正则替换。
"""

from __future__ import annotations

RELEASE_VERSION: str = "0.1.15-alpha"
SOURCE_COMMIT: str = "4bdaa13b8b406ee8048885f123a0c969724a61ae"
SOURCE_COMMIT_SHORT: str = "4bdaa13"
BUILDER_COMMIT: str = ""
