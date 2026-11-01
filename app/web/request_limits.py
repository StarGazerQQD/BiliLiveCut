# ruff: noqa: D100
from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.http_limits import request_body_limit


class RequestBodyTooLarge(HTTPException):
    """累计请求体越界，允许标准异常处理器返回明确的 413。"""

    def __init__(self) -> None:
        super().__init__(413, "请求体超过此接口的大小限制")


class RequestBodyLimitMiddleware:
    """在 JSON 和表单解析之前限制实际字节数，包括无声明长度的请求。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """逐块核对 HTTP 请求大小，保留其他 ASGI 协议的原始处理。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = request_body_limit(scope["path"], scope["method"])
        lengths = [value for name, value in scope["headers"] if name.lower() == b"content-length"]
        try:
            if len(lengths) > 1:
                raise ValueError("multiple lengths")
            length = int(lengths[0]) if lengths else 0
            if length < 0:
                raise ValueError("negative length")
        except ValueError:
            await JSONResponse({"detail": "Content-Length 无效"}, status_code=400)(scope, receive, send)
            return
        if length > limit:
            await JSONResponse({"detail": "请求体超过此接口的大小限制"}, status_code=413)(scope, receive, send)
            return
        total = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise RequestBodyTooLarge
            return message

        async def response_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, response_send)
        except RequestBodyTooLarge:
            if started:
                raise
            await JSONResponse({"detail": "请求体超过此接口的大小限制"}, status_code=413)(scope, receive, send)
