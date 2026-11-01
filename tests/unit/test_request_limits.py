from __future__ import annotations

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.core import http_limits
from app.web.request_limits import RequestBodyLimitMiddleware

if TYPE_CHECKING:
    from pytest import MonkeyPatch


async def consume(request: Request) -> JSONResponse:
    body = await request.body()
    return JSONResponse({"size": len(body)})


@pytest.mark.parametrize("declared", [False, True])
def test_management_body_limit_applies_to_actual_stream(declared: bool, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(http_limits, "MAX_MANAGEMENT_BYTES", 8)
    app = Starlette(routes=[Route("/api/manage", consume, methods=["POST"])])
    app.add_middleware(RequestBodyLimitMiddleware)

    def chunks() -> Iterator[bytes]:
        yield b"12345"
        yield b"67890"

    with TestClient(app) as client:
        response = client.post("/api/manage", content=b"1234567890" if declared else chunks())
        assert response.status_code == 413, response.text
        assert client.post("/api/manage", content=b"12345678").status_code == 200


def test_video_and_comment_paths_keep_their_existing_limits() -> None:
    identity = "a" * 32
    root = f"/api/recording-imports/{identity}/files"
    assert http_limits.request_body_limit(root + "/video", "PUT") == 50 * 1024**3
    assert http_limits.request_body_limit(root + "/comments", "PUT") == 32 * 1024**2
    assert http_limits.request_body_limit(root + "/video", "POST") == http_limits.MAX_MANAGEMENT_BYTES
    assert http_limits.request_body_limit("/api/not-an-upload/files/video", "PUT") == http_limits.MAX_MANAGEMENT_BYTES
    assert http_limits.request_body_limit("/auth/login", "POST") == 4096


def test_editor_limits_preserve_existing_unicode_capacity() -> None:
    corrected = json.dumps(
        {"corrected_text": "😀" * 200000, "aliases": {"😀" * 77 + str(i): "😀" * 80 for i in range(500)}}
    )
    room = json.dumps(
        {
            "room_config": {
                **{key: ["😀" * 80] * 500 for key in ("hotwords", "highlight_keywords", "blocked_topics")},
                **{key: {"😀" * 77 + str(i): "😀" * 80 for i in range(500)} for key in ("aliases", "learned_aliases")},
            }
        }
    )
    assert (
        http_limits.MAX_MANAGEMENT_BYTES
        < len(corrected.encode())
        <= http_limits.request_body_limit("/api/transcripts/1", "PATCH")
    )
    assert (
        http_limits.MAX_MANAGEMENT_BYTES < len(room.encode()) <= http_limits.request_body_limit("/api/rooms/1", "PATCH")
    )
    assert http_limits.request_body_limit("/api/rooms/1", "POST") == http_limits.MAX_MANAGEMENT_BYTES
    assert http_limits.request_body_limit("/api/rooms/1/other", "PATCH") == http_limits.MAX_MANAGEMENT_BYTES


@pytest.mark.parametrize(
    ("path", "method"), [("/api/model-configuration", "PUT"), ("/api/model-configuration/test", "POST")]
)
def test_model_catalog_has_a_separate_bounded_json_capacity(path: str, method: str) -> None:
    assert http_limits.request_body_limit(path, method) == 8 * 1024**2
    assert http_limits.request_body_limit(path + "/other", method) == http_limits.MAX_MANAGEMENT_BYTES
