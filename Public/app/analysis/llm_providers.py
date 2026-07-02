"""多大模型(LLM)配置管理:支持同时配置多个服务商并按优先级失败回退。

境内运行时单一服务商可能不稳定,故允许用户配置多组 OpenAI 兼容的
``(名称, base_url, api_key, model, 联网参数, 优先级, 启用)``,系统按**优先级从高到低**
依次调用;当前一个不可用/报错时自动降级到下一个。

存储:整份配置以 JSON 存于 ``app_settings`` 表的 ``llm_providers`` 键(可在 Web 后台
增删改),便于跨重启持久化。**若未配置任何多模型条目,则回退到 ``.env`` 的单模型
``LLM_*`` 配置**,保持向后兼容。

安全:对外(API/日志)展示时对 ``api_key`` 做掩码;保存时若未提供新 key,则按条目
``id`` 保留原有 key,避免前端回显掩码把真实 key 覆盖。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from loguru import logger

from app.core import settings_store
from app.core.config import settings

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
    api_key: str
    model: str
    web_search_param: str = ""
    price_input_per_m: float = 0.0
    price_output_per_m: float = 0.0
    enabled: bool = True
    priority: int = 100

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

        :returns: 字典(含 ``api_key_set`` / ``api_key_hint``)。
        """
        d = self.to_dict()
        d.pop("api_key")
        d["api_key_set"] = bool(self.api_key)
        d["api_key_hint"] = f"****{self.api_key[-4:]}" if self.api_key else ""
        return d


def _new_id() -> str:
    """生成短随机 id。

    :returns: 8 位十六进制串。
    """
    return uuid.uuid4().hex[:8]


def _coerce(raw: dict) -> LLMProvider | None:
    """把持久化字典转为 :class:`LLMProvider`(健壮容错)。

    :param raw: 原始字典。
    :returns: provider;缺少 base_url/model 时返回 ``None``。
    """
    if not isinstance(raw, dict):
        return None
    base_url = str(raw.get("base_url", "")).strip()
    model = str(raw.get("model", "")).strip()
    if not base_url or not model:
        return None

    def _f(key: str) -> float:
        try:
            return float(raw.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _i(key: str, default: int) -> int:
        try:
            return int(raw.get(key, default))
        except (TypeError, ValueError):
            return default

    return LLMProvider(
        id=str(raw.get("id") or _new_id()),
        name=str(raw.get("name", "")).strip() or model,
        base_url=base_url,
        api_key=str(raw.get("api_key", "")),
        model=model,
        web_search_param=str(raw.get("web_search_param", "")).strip(),
        price_input_per_m=_f("price_input_per_m"),
        price_output_per_m=_f("price_output_per_m"),
        enabled=bool(raw.get("enabled", True)),
        priority=_i("priority", 100),
    )


def _read_raw() -> list[dict]:
    """从存储读取原始 provider 列表。

    :returns: 字典列表(可能为空)。
    """
    text = settings_store.get_setting(_STORE_KEY, "")
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("llm_providers 配置解析失败,忽略。")
        return []
    return data if isinstance(data, list) else []


def _from_settings() -> LLMProvider | None:
    """把 ``.env`` 的单模型 ``LLM_*`` 配置构造为一个 provider(向后兼容)。

    :returns: provider;未配置 key 时返回 ``None``。
    """
    api_key = settings.llm_api_key or settings.anthropic_api_key
    if not api_key:
        return None
    return LLMProvider(
        id="env",
        name=settings.llm_provider or "env",
        base_url=settings.llm_base_url,
        api_key=api_key,
        model=settings.llm_model or settings.anthropic_model or "deepseek-chat",
        web_search_param=settings.llm_web_search_param,
        price_input_per_m=settings.llm_price_input_per_m,
        price_output_per_m=settings.llm_price_output_per_m,
        enabled=True,
        priority=100,
    )


def load_providers() -> list[LLMProvider]:
    """加载全部 provider,按优先级升序排序;为空时回退到 ``.env`` 单模型。

    :returns: provider 列表。
    """
    items = [p for p in (_coerce(r) for r in _read_raw()) if p is not None]
    if not items:
        env = _from_settings()
        return [env] if env is not None else []
    return sorted(items, key=lambda p: (p.priority, p.name))


def active_providers() -> list[LLMProvider]:
    """返回可用的 provider(已启用且配置了 key),按优先级升序。

    :returns: 可用 provider 列表。
    """
    return [p for p in load_providers() if p.enabled and p.api_key]


def public_view() -> list[dict]:
    """返回对外视图列表(key 掩码)。

    :returns: 字典列表。
    """
    return [p.public_dict() for p in load_providers()]


def save_providers(items: list[LLMProvider]) -> None:
    """持久化 provider 列表(明文 key)。

    :param items: provider 列表。
    """
    settings_store.set_setting(
        _STORE_KEY, json.dumps([p.to_dict() for p in items], ensure_ascii=False)
    )
    logger.info("已保存 {} 个大模型配置。", len(items))


def merge_and_save(incoming: list[dict]) -> list[dict]:
    """合并保存前端提交的配置:未提供新 key 的条目沿用旧 key。

    :param incoming: 前端提交的 provider 字典列表(``api_key`` 可为空表示不修改)。
    :returns: 保存后的对外视图列表。
    """
    existing = {r.get("id"): r for r in _read_raw() if r.get("id")}
    merged: list[LLMProvider] = []
    for raw in incoming:
        pid = str(raw.get("id") or "").strip() or _new_id()
        data = dict(raw)
        data["id"] = pid
        # 未提供新 key(空/仅掩码占位)时,沿用旧 key。
        new_key = str(raw.get("api_key") or "").strip()
        if not new_key or new_key.startswith("****"):
            data["api_key"] = existing.get(pid, {}).get("api_key", "")
        prov = _coerce(data)
        if prov is not None:
            merged.append(prov)
    save_providers(merged)
    return public_view()
