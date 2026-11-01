# ruff: noqa: D100
from __future__ import annotations

import re

MAX_VIDEO_BYTES = 50 * 1024**3
MAX_COMMENT_BYTES = 32 * 1024**2
MAX_MANAGEMENT_BYTES = 2 * 1024**2
MAX_EDITOR_BYTES = 4 * 1024**2
MAX_MODEL_CONFIGURATION_BYTES = 8 * 1024**2
_IMPORT_FILE = re.compile(r"/api/recording-imports/[0-9a-f]{32}/files/(video|comments)\Z")
_EDITOR = re.compile(r"/api/(transcripts|rooms)/[0-9]+\Z")


def request_body_limit(path: str, method: str) -> int:
    """为实际上传路由保留业务上限，其余管理请求使用小容量限制。"""
    match = _IMPORT_FILE.fullmatch(path)
    if method == "PUT" and match is not None:
        return MAX_VIDEO_BYTES if match.group(1) == "video" else MAX_COMMENT_BYTES
    if method == "PATCH" and _EDITOR.fullmatch(path) is not None:
        return MAX_EDITOR_BYTES
    if (method == "PUT" and path == "/api/model-configuration") or (
        method == "POST" and path == "/api/model-configuration/test"
    ):
        return MAX_MODEL_CONFIGURATION_BYTES
    if path.startswith("/auth/"):
        return 4096
    return MAX_MANAGEMENT_BYTES
