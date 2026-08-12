"""Portable Release 元数据 — 由 Payload 构建时写入，运行时读取。

业务代码通过此文件读取 RELEASE_VERSION 和 SOURCE_COMMIT，
避免对 README/CHANGELOG 等历史文档执行宽泛正则替换。
"""

from __future__ import annotations

RELEASE_VERSION: str = "0.1.17.3-alpha"
SOURCE_COMMIT: str = "92618efcb9a3d6ec33f5ce3e0b2f46ac7e2cf55a"
SOURCE_COMMIT_SHORT: str = "92618ef"
BUILDER_COMMIT: str = ""
