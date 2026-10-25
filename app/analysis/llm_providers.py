"""多大模型(LLM)配置管理:支持同时配置多个服务商并按优先级失败回退。

境内运行时单一服务商可能不稳定,故允许用户配置多组 OpenAI 兼容的
``(名称, base_url, api_key, model, 联网参数, 优先级, 启用)``,系统按**优先级从高到低**
依次调用;当前一个不可用/报错时自动降级到下一个。

存储:新配置由模型目录管理连接和业务路由；旧 ``llm_providers`` 配置保留读取与
写入兼容，转换为独立路由后禁止旧整表写入。没有启用的条目时，大模型能力保持不可用。

安全:对外(API/日志)展示时对 ``api_key`` 做掩码;保存时若未提供新 key,则按条目
``id`` 保留原有 key,避免前端回显掩码把真实 key 覆盖。
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

from loguru import logger
from sqlmodel import Session

from app.core import settings_store
from app.core.config import settings
from app.core.configuration import configuration_transaction
from app.db.entities import AppSetting
from app.models.configuration import MODEL_CONFIGURATION_KEY, authorize_model_call, configured_models
from app.models.contracts import TEXT_CAPABILITIES, ModelConnection, ModelRoute, TextCapability

_STORE_KEY = "llm_providers"


@dataclass(slots=True)
class LLMProvider:
    """单个大模型服务商配置。

    :param id: 稳定标识(用于保存时保留 key)。
    :param name: 展示名称。
    :param base_url: OpenAI 兼容 API 的 base_url。
    :param api_key: API Key。
    :param model: 模型名。
    :param web_search_param: 联网搜索开关键名(以 extra_body 传入);空表示不尝试。
    :param price_input_per_m: 输入价格(每百万 token),用于预算估算。
    :param price_output_per_m: 输出价格(每百万 token)。
    :param enabled: 是否启用。
    :param priority: 优先级(数字越小越优先)。
    """

    id: str
    name: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    web_search_param: str = ""
    price_input_per_m: float = 0.0
    price_output_per_m: float = 0.0
    enabled: bool = True
    priority: int = 100
    connection_id: str = ""
    max_output_tokens: int | None = None
    authorize: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        """序列化为可持久化字典(含明文 key)。

        :returns: 字典。
        """
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "web_search_param": self.web_search_param,
            "price_input_per_m": self.price_input_per_m,
            "price_output_per_m": self.price_output_per_m,
            "enabled": self.enabled,
            "priority": self.priority,
        }

    def public_dict(self) -> dict:
        """序列化为对外视图(key 掩码,不泄露明文)。

        :returns: 字典(含 ``api_key_set`` 标志)。
        """
        d = self.to_dict()
        d.pop("api_key")
        d["api_key_set"] = bool(self.api_key)
        return d


def _new_id() -> str:
    """生成短随机 id。

    :returns: 8 位十六进制串。
    """
    return uuid.uuid4().hex[:8]


_PROVIDER_FIELDS = {
    "id",
    "name",
    "base_url",
    "api_key",
    "model",
    "web_search_param",
    "price_input_per_m",
    "price_output_per_m",
    "enabled",
    "priority",
}


def _coerce(raw: dict) -> LLMProvider:
    """严格解析当前格式的持久化 LLM 配置。

    :param raw: 原始字典。
    :returns: provider。
    :raises ValueError: 字段缺失、未知或类型错误。
    """
    if not isinstance(raw, dict):
        raise ValueError("LLM provider 必须是对象")
    missing = _PROVIDER_FIELDS - set(raw)
    unknown = set(raw) - _PROVIDER_FIELDS
    if missing:
        raise ValueError(f"LLM provider 缺少字段: {sorted(missing)}")
    if unknown:
        raise ValueError(f"LLM provider 包含未知字段: {sorted(unknown)}")
    for field_name in ("id", "name", "base_url", "api_key", "model", "web_search_param"):
        if not isinstance(raw[field_name], str):
            raise ValueError(f"LLM provider {field_name} 必须是字符串")
    base_url = raw["base_url"].strip()
    model = raw["model"].strip()
    provider_id = raw["id"].strip()
    name = raw["name"].strip()
    if not provider_id or not name or not base_url or not model:
        raise ValueError("LLM provider id/name/base_url/model 不能为空")
    if not isinstance(raw["enabled"], bool):
        raise ValueError("LLM provider enabled 必须是布尔值")
    for field_name in ("price_input_per_m", "price_output_per_m"):
        if not isinstance(raw[field_name], (int, float)) or isinstance(raw[field_name], bool):
            raise ValueError(f"LLM provider {field_name} 必须是数字")
        if not math.isfinite(raw[field_name]) or raw[field_name] < 0:
            raise ValueError(f"LLM provider {field_name} 必须为有限非负数")
    if not isinstance(raw["priority"], int) or isinstance(raw["priority"], bool):
        raise ValueError("LLM provider priority 必须是整数")

    return LLMProvider(
        id=provider_id,
        name=name,
        base_url=base_url,
        api_key=raw["api_key"],
        model=model,
        web_search_param=raw["web_search_param"].strip(),
        price_input_per_m=raw["price_input_per_m"],
        price_output_per_m=raw["price_output_per_m"],
        enabled=raw["enabled"],
        priority=raw["priority"],
    )


def _read_raw(db: Session | None = None) -> list[dict]:
    """从存储读取原始 provider 列表。

    :returns: 字典列表(可能为空)。
    """
    row = db.get(AppSetting, _STORE_KEY) if db is not None else None
    text = (row.value if row is not None else "") if db is not None else settings_store.get_setting(_STORE_KEY, "")
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("llm_providers 不是有效 JSON") from exc
    if not isinstance(data, list):
        raise ValueError("llm_providers 必须是数组")
    return data


def from_route(route: ModelRoute, connection: ModelConnection) -> LLMProvider:
    """把已解析的文本路由适配为既有调用契约，连接身份独立于模型。"""
    return LLMProvider(
        id=route.id,
        name=route.name,
        base_url=connection.base_url,
        api_key=connection.api_key,
        model=route.model,
        web_search_param=route.web_search_param,
        price_input_per_m=route.price_input_per_m,
        price_output_per_m=route.price_output_per_m,
        enabled=route.enabled and connection.enabled,
        priority=route.priority,
        connection_id=connection.id,
        max_output_tokens=route.max_output_tokens,
    )


def load_providers(capability: TextCapability = "analysis") -> list[LLMProvider]:
    """加载指定文字能力的路由，默认高光分析，按优先级升序排序。

    :returns: provider 列表。
    """
    if capability not in TEXT_CAPABILITIES:
        raise ValueError("文字生成仅接受文字业务能力，ASR 必须使用语音转写入口")
    config = configured_models()
    if config is not None:
        connections = {item.id: item for item in config.connections}
        items = [
            from_route(route, connections[route.connection_id])
            for route in config.routes
            if route.capability == capability
        ]
    else:
        items = [_coerce(raw) for raw in _read_raw()]
        if capability == "trends" and settings.trend_api_key and settings.trend_base_url and settings.trend_model:
            items.insert(
                0,
                LLMProvider(
                    id="trend",
                    name="网感采集专用",
                    base_url=settings.trend_base_url,
                    api_key=settings.trend_api_key,
                    model=settings.trend_model,
                    web_search_param=settings.trend_web_search_param,
                    priority=min((item.priority for item in items), default=100) - 1,
                    authorize=partial(
                        authorize_model_call,
                        "dedicated-trends",
                        "dedicated-trends",
                        settings.trend_base_url,
                        settings.trend_api_key,
                    ),
                ),
            )
    for provider in items:
        if provider.authorize is not None:
            continue
        connection_id = provider.connection_id or f"legacy-{provider.id}"
        route_id = provider.id if provider.connection_id else f"{capability}-{provider.id}"
        provider.authorize = partial(authorize_model_call, connection_id, route_id, provider.base_url, provider.api_key)
    return sorted(items, key=lambda p: (p.priority, p.name))


def active_providers(capability: TextCapability = "analysis") -> list[LLMProvider]:
    """返回已启用的能力路由；新连接允许无鉴权服务，旧配置仍要求密钥。

    :returns: 可用 provider 列表。
    """
    return [p for p in load_providers(capability) if p.enabled and (p.api_key or p.connection_id) and p.base_url]


def public_view() -> list[dict]:
    """返回对外视图列表(key 掩码)。

    :returns: 字典列表。
    """
    return [p.public_dict() for p in load_providers()]


def save_providers(items: list[LLMProvider]) -> None:
    """持久化 provider 列表(明文 key)。

    :param items: provider 列表。
    """
    with configuration_transaction() as (db, _revision):
        _save_legacy(db, items)
    logger.info("已保存 {} 个大模型配置。", len(items))


def _save_legacy(db: Session, items: list[LLMProvider]) -> None:
    if db.get(AppSetting, MODEL_CONFIGURATION_KEY) is not None:
        raise ValueError("已启用独立业务路由，请使用模型连接与路由页面；旧整表接口不能覆盖新配置")
    row = db.get(AppSetting, _STORE_KEY) or AppSetting(key=_STORE_KEY, value="")
    row.value = json.dumps([p.to_dict() for p in items], ensure_ascii=False, allow_nan=False)
    db.add(row)


def merge_providers(incoming: list[dict], *, db: Session | None = None) -> list[LLMProvider]:
    """合并前端配置与已保存密钥，但不写入存储。

    :param incoming: 前端提交的 provider 字典列表(``api_key`` 可为空表示不修改)。
    :returns: 合并并按优先级排序的 provider 列表。
    """
    existing = {str(raw["id"]): raw for raw in _read_raw(db)}
    merged: list[LLMProvider] = []
    seen: set[str] = set()
    for item in incoming:
        raw = dict(item)
        clear_key = raw.pop("clear_api_key", False)
        if not isinstance(clear_key, bool):
            raise ValueError("clear_api_key 必须为布尔值")
        if set(raw) != _PROVIDER_FIELDS:
            missing = sorted(_PROVIDER_FIELDS - set(raw))
            unknown = sorted(set(raw) - _PROVIDER_FIELDS)
            raise ValueError(f"LLM provider 字段不匹配: missing={missing} unknown={unknown}")
        pid = str(raw["id"]).strip() or _new_id()
        if pid in seen:
            raise ValueError("LLM provider ID 不能重复")
        seen.add(pid)
        data = dict(raw)
        data["id"] = pid
        # 未提供新 key(空/仅掩码占位)时,沿用旧 key。
        new_key = str(raw["api_key"]).strip()
        if clear_key:
            data["api_key"] = ""
        elif not new_key or new_key.startswith("****"):
            old = existing.get(pid)
            if (
                old
                and old.get("api_key")
                and str(old["base_url"]).rstrip("/") != str(raw["base_url"]).strip().rstrip("/")
            ):
                raise ValueError("修改服务地址时必须重新填写密钥或明确清空")
            data["api_key"] = existing.get(pid, {}).get("api_key", "")
        merged.append(_coerce(data))
    return sorted(merged, key=lambda p: (p.priority, p.name))


def merge_and_save(incoming: list[dict]) -> list[dict]:
    """合并保存前端提交的配置:未提供新 key 的条目沿用旧 key。

    :param incoming: 前端提交的 provider 字典列表(``api_key`` 可为空表示不修改)。
    :returns: 保存后的对外视图列表。
    """
    with configuration_transaction() as (db, _revision):
        merged = merge_providers(incoming, db=db)
        _save_legacy(db, merged)
    return [p.public_dict() for p in merged]
