# ruff: noqa: D100
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TextCapability = Literal["analysis", "transcript", "trends", "summary", "copywriting"]
Capability = Literal[TextCapability, "asr"]
TEXT_CAPABILITIES: tuple[TextCapability, ...] = ("analysis", "transcript", "trends", "summary", "copywriting")
MAX_CONFIGURABLE_TOKENS = 2_097_152
Identity = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x20\x7F]+$")]
Name = Annotated[
    str, Field(min_length=1, max_length=200, pattern=r"^[^\s\x00-\x1F\x7F](?:[^\x00-\x1F\x7F]*[^\s\x00-\x1F\x7F])?$")
]


class ModelConnection(BaseModel):
    """可供多个业务路由引用的 OpenAI 兼容服务连接。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True, allow_inf_nan=False)
    id: Identity
    name: Name
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(default="", max_length=4096, pattern=r"^[!-~]*$", repr=False)
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        """允许管理员明确选择本地服务，拒绝 URL 内凭据、查询参数及控制字符。"""
        if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
            raise ValueError("服务地址含空白或非法字符")
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            raise ValueError("服务地址无效") from None
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or port == 0
        ):
            raise ValueError("服务地址必须为无凭据、查询参数和片段的 HTTP(S) 基础地址")
        return value.rstrip("/")


class ConnectionChange(ModelConnection):
    """空密钥保留同一连接的已保存凭据，清空必须明确声明。"""

    clear_api_key: bool = False


class ModelRoute(BaseModel):
    """业务能力独立选择模型、连接、调用顺序和计费参数。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True, allow_inf_nan=False)
    id: Identity
    name: Name
    capability: Capability
    connection_id: Identity
    model: Name
    enabled: bool = True
    priority: int = Field(default=100, ge=-1000000, le=1000000)
    web_search_param: str = Field(default="", max_length=64, pattern=r"^(?:[A-Za-z][A-Za-z0-9_]*)?$")
    context_window_tokens: int | None = Field(default=None, ge=1024, le=MAX_CONFIGURABLE_TOKENS)
    max_input_tokens: int | None = Field(default=None, ge=128, le=MAX_CONFIGURABLE_TOKENS)
    max_output_tokens: int | None = Field(default=None, ge=16, le=MAX_CONFIGURABLE_TOKENS)
    price_input_per_m: float = Field(default=0.0, ge=0)
    price_output_per_m: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def validate_parameters(self) -> ModelRoute:
        """搜索参数不得覆盖协议字段，转写路由不接受文本生成参数。"""
        if self.web_search_param in {
            "model",
            "messages",
            "stream",
            "tools",
            "api_key",
            "max_tokens",
            "max_completion_tokens",
        }:
            raise ValueError("搜索开关键不能覆盖协议字段")
        if self.capability == "asr" and (
            self.web_search_param
            or self.context_window_tokens is not None
            or self.max_input_tokens is not None
            or self.max_output_tokens is not None
            or self.price_input_per_m
            or self.price_output_per_m
        ):
            raise ValueError("ASR 路由不接受搜索、文本 token 上限或文本计费参数")
        return self


class ModelConfiguration(BaseModel):
    """完整模型目录；孤立引用和重复标识在持久化之前拒绝。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    connections: list[ModelConnection] = Field(default_factory=list, max_length=64)
    routes: list[ModelRoute] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_references(self) -> ModelConfiguration:
        """所有路由必须指向目录内的服务连接，ID 必须唯一。"""
        connections = {item.id for item in self.connections}
        if len(connections) != len(self.connections) or len({item.id for item in self.routes}) != len(self.routes):
            raise ValueError("模型连接或路由 ID 不能重复")
        if any(route.connection_id not in connections for route in self.routes):
            raise ValueError("模型路由引用了不存在的服务连接")
        return self


class ModelConfigurationChange(BaseModel):
    """模型配置整体保存，与其他设置共用乐观版本检查。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    revision: int = Field(ge=0)
    connections: list[ConnectionChange] = Field(max_length=64)
    routes: list[ModelRoute] = Field(max_length=256)


class ModelProbe(ModelConfigurationChange):
    """仅测试管理员明确选择的文本路由，不保存草稿。"""

    route_id: Identity


class ConnectionView(BaseModel):
    """服务连接的公开投影，永不包含密钥。"""

    id: str
    name: str
    base_url: str
    enabled: bool
    api_key_set: bool


class ModelConfigurationView(BaseModel):
    """同一数据库版本的连接、路由及兼容来源。"""

    revision: int
    source: Literal["legacy", "routes"]
    connections: list[ConnectionView]
    routes: list[ModelRoute]
    active_counts: dict[Capability, int]
    asr_selection: str
    model_capacities: list[ModelCapacity] = Field(default_factory=list)
    probe_diagnostics: list[ModelProbeDiagnostic] = Field(default_factory=list)


class ModelCapacity(BaseModel):
    """按原厂端点和精确型号核查的能力；未公布的限制保留为空。"""

    model_config = ConfigDict(frozen=True)
    id: str
    base_urls: tuple[str, ...]
    models: tuple[str, ...]
    context_window_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    output_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    reasoning_reserve_tokens: int = 0
    sources: tuple[str, ...]
    checked_on: str = "2026-10-29"
    note: str = ""


class ModelProbeDiagnostic(BaseModel):
    """有限样本的实际协议观察，不推断服务最大容量或参数执行效果。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1
    route_id: Identity
    connection_id: Identity
    model: Name
    base_url: str = Field(min_length=1, max_length=2048)
    observed_at: datetime
    text: Literal["unknown", "accepted", "rejected"]
    structured: Literal["unknown", "accepted", "rejected"]
    structured_protocol: Literal["json_schema", "json_object"]
    reasoning_parameter: Literal["not_sent", "unknown", "accepted", "rejected"]
    attempts: int = Field(ge=0, le=2)
    output_limits: list[int] = Field(default_factory=list, max_length=2)
    request_ids: list[str] = Field(default_factory=list, max_length=2)
    elapsed_s: float = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    detail: str = Field(min_length=1, max_length=500)


class ModelProbeResult(BaseModel):
    """单条文本路由的有限调用结果。"""

    id: str
    ok: bool
    detail: str
    diagnostic: ModelProbeDiagnostic | None = None
    persisted: bool = False
