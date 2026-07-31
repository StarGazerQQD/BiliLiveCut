"""Portable Release 元数据 — 由 Payload 构建时写入，运行时读取。

业务代码通过此文件读取 RELEASE_VERSION 和 SOURCE_COMMIT，
避免对 README/CHANGELOG 等历史文档执行宽泛正则替换。
"""

from __future__ import annotations

RELEASE_VERSION: str = "0.1.16.2-alpha"
SOURCE_COMMIT: str = "6939e68288e00a5af7eb7999f48b5002c0b5c869"
SOURCE_COMMIT_SHORT: str = "6939e68"
BUILDER_COMMIT: str = ""
