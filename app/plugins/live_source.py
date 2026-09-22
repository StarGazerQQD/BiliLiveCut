"""直播源公共契约 v1；插件只依赖此模块及 ``app.plugins``。"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

LIVE_SOURCE_API_VERSION = "1"
_PLATFORM_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def http_url(value: str) -> str:
    """只接受不带内嵌凭据、空白或控制字符的 HTTP(S) URL。"""
    parsed = urlsplit(value)
    # 读取 port 触发 urllib 对非法端口的校验。
    _ = parsed.port
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in value
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("来源地址必须是有效 HTTP(S) URL，且不能包含内嵌凭据或控制字符")
    return value


def utc_datetime(value: datetime) -> datetime:
    """要求显式时区并转换成 UTC，禁止猜测插件本地时钟。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(UTC)


class _Contract(BaseModel):
    """不允许未声明字段的只读公共模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceDescriptor(_Contract):
    """注册信息；domains 为精确匹配的主机名，短链接域名也必须显式声明。"""

    platform: str = Field(pattern=_PLATFORM_PATTERN)
    name: str = Field(min_length=1, max_length=80)
    domains: tuple[str, ...] = Field(min_length=1)

    @field_validator("platform")
    @classmethod
    def exclude_local(cls, value: str) -> str:
        """保留本地录播命名空间。"""
        if value == "local":
            raise ValueError("local 保留给宿主本地录播")
        return value

    @field_validator("domains")
    @classmethod
    def validate_domains(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """拒绝通配符、路径和端口；同一域名只能有一个注册归属。"""
        for value in values:
            if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", value):
                raise ValueError("domains 必须是小写精确主机名")
        if len(values) != len(set(values)):
            raise ValueError("domains 不能重复")
        return values


class SourceRoom(_Contract):
    """平台稳定身份；source_id 与宿主数据库主键、主播 ID、场次 ID 无关。"""

    platform: str = Field(pattern=_PLATFORM_PATTERN)
    source_id: str = Field(min_length=1, max_length=512, strict=True)
    canonical_url: str = Field(max_length=2048)

    @field_validator("source_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        """禁止空白边界和控制字符，保留不透明字符串的原始语义。"""
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("source_id 包含无效空白或控制字符")
        return value

    @field_validator("canonical_url")
    @classmethod
    def validate_canonical(cls, value: str) -> str:
        """规范地址必须可持久化，不得带临时鉴权查询参数或 fragment。"""
        http_url(value)
        if urlsplit(value).query or urlsplit(value).fragment:
            raise ValueError("canonical_url 不能包含查询参数或 fragment")
        return value


class LiveStatus(StrEnum):
    """未知状态不能被宿主解释为下播。"""

    LIVE = "live"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class RoomSnapshot(_Contract):
    """一次状态观察；缺失标题或主播信息保留为 None。"""

    status: LiveStatus
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    uploader_name: str | None = Field(default=None, min_length=1, max_length=200)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _validate_time = field_validator("observed_at")(utc_datetime)


class StreamPreference(_Contract):
    """通用选择意图；平台清晰度编码仅由所属适配器解释。"""

    intent: Literal["best", "balanced", "data_saver"] = "best"
    preferred_transport: Literal["hls", "flv"] = "hls"


class StreamSpec(_Contract):
    """内存内的临时播放凭据；列表顺序由来源按 preference 排好。"""

    url: str = Field(repr=False, max_length=16384)
    transport: Literal["hls", "flv"]
    container: Literal["ts", "fmp4", "flv"]
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    quality_id: str | None = Field(default=None, max_length=80)
    quality_label: str | None = Field(default=None, max_length=120)
    codec: str | None = Field(default=None, max_length=80)
    expires_at: datetime | None = None

    _validate_url = field_validator("url")(http_url)

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        """拒绝换行注入及转发控制头，宿主不接受任何额外 FFmpeg 参数。"""
        seen: set[str] = set()
        if sum(len(key) + len(value) for key, value in headers.items()) > 32768:
            raise ValueError("请求头过大")
        for key, value in headers.items():
            name = key.lower()
            if (
                not _HEADER_NAME.fullmatch(key)
                or name in seen
                or name in {"host", "content-length", "transfer-encoding", "connection", "proxy-authorization"}
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise ValueError("来源请求头无效")
            seen.add(name)
        return dict(headers)

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        """过期时间与宿主 UTC 时钟比较。"""
        return utc_datetime(value) if value is not None else None


class SourceError(RuntimeError):
    """来源错误基类；message 必须可公开，不能包含播放地址、Cookie 或 token。"""

    code = "source_error"

    def __init__(self, message: str = "直播源调用失败", *, retry_after: float | None = None) -> None:
        if retry_after is not None and (not math.isfinite(retry_after) or retry_after < 0):
            raise ValueError("retry_after 必须为非负有限秒数")
        self.retry_after = retry_after
        super().__init__(message)


class SourceTemporaryError(SourceError):
    """暂时网络或服务故障，可在宿主预算内重试。"""

    code = "temporary"


class SourceRateLimited(SourceTemporaryError):
    """平台限流；宿主在 retry_after 窗口内停止调用该来源。"""

    code = "rate_limited"


class SourceAuthenticationError(SourceError):
    """凭据失效；需要管理员处理，不能反复重试。"""

    code = "authentication"


class SourceInvalidInput(SourceError):
    """无法解析输入或房间身份无效。"""

    code = "invalid_input"


class SourceUnavailable(SourceError):
    """插件未启用、正在停用或服务明确不可用。"""

    code = "unavailable"


@runtime_checkable
class LiveSource(Protocol):
    """异步来源；方法须可取消，同步 SDK 必须自行隔离并设置有限 I/O 超时。"""

    descriptor: SourceDescriptor

    async def resolve_room(self, value: str) -> SourceRoom:
        """解析含短链接的输入，返回不随开播场次变化的规范身份。"""

    async def get_room_info(self, room: SourceRoom) -> RoomSnapshot:
        """查询明确状态；失败抛出 SourceError，不能伪造 OFFLINE。"""

    async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        """返回新播放凭据，按偏好排序；禁止长期缓存签名 URL。"""

    async def aclose(self) -> None:
        """幂等释放全部来源资源；由宿主在使用者收尾后调用。"""


class DanmakuStatus(StrEnum):
    """场次证据状态；AVAILABLE 表示实际已连通，即使事件数量为零。"""

    UNSUPPORTED = "unsupported"
    DISABLED = "disabled"
    CONNECTING = "connecting"
    AVAILABLE = "available"
    FAILED = "failed"


class DanmakuEvent(_Contract):
    """跨平台事件只接收普通文本计数；原始礼物金额、人气不具有统一语义。"""

    occurred_at: datetime
    content: str = Field(min_length=1, max_length=10000)
    user_name: str | None = Field(default=None, max_length=200)

    _validate_time = field_validator("occurred_at")(utc_datetime)


DanmakuSink = Callable[[DanmakuEvent], Awaitable[None]]
DanmakuStateSink = Callable[[DanmakuStatus], Awaitable[None]]


@runtime_checkable
class DanmakuSource(Protocol):
    """可选采集能力；宿主取消任务后方法须完成连接收尾再返回。"""

    async def collect_danmaku(self, room: SourceRoom, emit: DanmakuSink, state: DanmakuStateSink) -> None:
        """持续发送 UTC 事件及真实连接状态；无事件不能伪造采集失败。"""
