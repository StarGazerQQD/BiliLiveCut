"""Portable Release 元数据 — 由 Payload 构建时写入，运行时读取。

业务代码通过此文件读取 RELEASE_VERSION 和 SOURCE_COMMIT，
避免对 README/CHANGELOG 等历史文档执行宽泛正则替换。
"""

from __future__ import annotations

RELEASE_VERSION: str = "0.1.17.3-alpha"
SOURCE_COMMIT: str = "e4fa0260d21ad65e30be03b01f2fd5ef77679f19"
SOURCE_COMMIT_SHORT: str = "e4fa026"
BUILDER_COMMIT: str = ""
