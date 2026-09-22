"""独立直播源示例：只导入公开契约，连接本地测试 HTTP 服务。"""

from __future__ import annotations

from urllib.parse import quote, urlsplit

import httpx

from app.plugins import BasePlugin, PluginContext, PluginSetting
from app.plugins.live_source import (
    RoomSnapshot,
    SourceAuthenticationError,
    SourceDescriptor,
    SourceInvalidInput,
    SourceRateLimited,
    SourceRoom,
    SourceTemporaryError,
    SourceUnavailable,
    StreamPreference,
    StreamSpec,
)


class ExampleSource:
    """共享异步连接池；每次请求读取本插件设置，不保留播放凭据。"""

    descriptor = SourceDescriptor(platform="example_live", name="本地契约示例", domains=("127.0.0.1", "localhost"))

    def __init__(self, context: PluginContext) -> None:
        self.context = context
        self.client = httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False)

    def _origin(self) -> str:
        value = self.context.get_setting("api_origin", "http://127.0.0.1:9901")
        if not isinstance(value, str):
            raise SourceInvalidInput("请在示例插件设置中填写本地 HTTP 服务地址")
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as exc:
            raise SourceInvalidInput("示例 HTTP 服务地址格式无效") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname not in self.descriptor.domains
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise SourceInvalidInput("示例仅接受 http://127.0.0.1:端口 或 http://localhost:端口")
        return value.rstrip("/")

    def _headers(self) -> dict[str, str]:
        value = self.context.get_setting("access_key", "")
        if not isinstance(value, str) or not value:
            raise SourceAuthenticationError("请在示例插件设置中填写测试访问密钥")
        # 复用公开模型校验控制字符，避免将不合法值交给 HTTP 客户端。
        return StreamSpec(
            url=self._origin(), transport="flv", container="flv", headers={"X-Example-Key": value}
        ).headers

    async def _get(self, path: str, params: dict[str, str] | None = None) -> object:
        try:
            response = await self.client.get(self._origin() + path, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise SourceTemporaryError("本地示例 HTTP 服务暂时不可达") from exc
        if response.status_code in {401, 403}:
            raise SourceAuthenticationError("本地示例服务拒绝了访问密钥")
        if response.status_code == 429:
            raise SourceRateLimited("本地示例服务限流", retry_after=2)
        if response.status_code >= 500:
            raise SourceTemporaryError("本地示例服务暂时故障")
        if response.status_code != 200:
            raise SourceInvalidInput("本地示例服务未找到房间或接口；请检查服务地址")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceUnavailable("本地示例服务返回了无效 JSON") from exc

    async def resolve_room(self, value: str) -> SourceRoom:
        """由示例 API 把裸 ID、短地址和完整 URL 解析为同一稳定字符串身份。"""
        payload = await self._get("/resolve", {"value": value})
        return SourceRoom.model_validate(payload)

    async def get_room_info(self, room: SourceRoom) -> RoomSnapshot:
        """服务必须区分明确下播、未知及 HTTP 查询失败。"""
        return RoomSnapshot.model_validate(await self._get(f"/rooms/{quote(room.source_id, safe='')}/info"))

    async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        """每次读取新候选；初始播放 URL 限同一 origin，后续媒体链须来自可信服务。"""
        payload = await self._get(
            f"/rooms/{quote(room.source_id, safe='')}/streams",
            {"intent": preference.intent, "transport": preference.preferred_transport},
        )
        if not isinstance(payload, list):
            raise SourceUnavailable("本地示例服务未返回播放候选列表")
        streams = [StreamSpec.model_validate(item) for item in payload]
        origin = urlsplit(self._origin())
        for stream in streams:
            target = urlsplit(stream.url)
            if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
                raise SourceUnavailable("示例拒绝向其它 origin 转发测试凭据")
        return [StreamSpec.model_validate({**item.model_dump(), "headers": self._headers()}) for item in streams]

    async def aclose(self) -> None:
        """来源归宿主管理；所有短请求和录制退出后幂等关闭连接池。"""
        await self.client.aclose()


class Plugin(BasePlugin):
    """可直接由正式插件加载器装载；示例不提供可选弹幕。"""

    settings_schema = (
        PluginSetting(key="api_origin", label="本地示例服务地址", default="http://127.0.0.1:9901", required=True),
        PluginSetting(key="access_key", label="测试访问密钥", kind="password", default=""),
    )

    def on_enable(self, context: PluginContext) -> None:
        """即使未配置凭据也可先启用，再通过宿主设置页面填写。"""
        context.register_live_source(ExampleSource(context))
