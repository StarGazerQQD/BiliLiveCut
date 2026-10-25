"""大模型(OpenAI 兼容协议)高光复核与文案能力的底层封装。

    系统主要在中国大陆境内运行,故 LLM 层采用
    **OpenAI 兼容协议**,可对接境内可稳定访问的服务商(DeepSeek / 通义千问 Qwen /
Moonshot Kimi / 智谱 GLM 等)，连接和业务路由统一在控制台配置。语音转写可明确选择本地或远程模型。

设计目标(对应"降低 AI 成本""优先稳定"):

* **可禁用**:没有启用的业务路由时跳过 LLM，由业务使用规则结果;
* **预算护栏**:可设每日花费上限(按可配置的 token 价格估算),超额自动降级;
* **失败回退**:服务请求失败时尝试下一路由，预算账本故障停止调用并报告;
* **依赖边界**:Portable 已内置 ``openai`` SDK;源码安装可通过
  ``pip install -e ".[llm]"`` 启用。

本模块承载文字业务的模型调用；语音转写使用独立协议和受监管子进程。
"""

from __future__ import annotations

import hmac
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

from filelock import FileLock, Timeout
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.analysis import llm_providers as provs
from app.core.config import settings
from app.core.paths import storage_root
from app.core.sanitize import safe_exception_summary
from app.models.contracts import TextCapability

if TYPE_CHECKING:
    from openai import OpenAI


def _daily_budget() -> float:
    """返回每日预算；0 表示不限额。"""
    return settings.llm_daily_budget


@dataclass(slots=True)
class HighlightJudgement:
    """LLM 对一个候选片段的高光判断结果。

    :param is_highlight: 是否值得切片传播。
    :param score: 高光置信度(0-1)。
    :param reason: 判断理由(简短)。
    :param suggested_start_offset: 建议起点相对片段起点的偏移(秒,可空)。
    :param suggested_end_offset: 建议终点偏移(秒,可空)。
    """

    is_highlight: bool
    score: float
    reason: str
    suggested_start_offset: float | None = None
    suggested_end_offset: float | None = None


@dataclass(slots=True, frozen=True)
class TranscriptRefinement:
    """LLM 对单个录制片段转写的整理结果。

    :param clean_text: 保留原意、补全标点和段落后的可读文本。
    :param summary: 对该片段内容的简短概括。
    """

    clean_text: str
    summary: str


def is_llm_enabled(capability: TextCapability = "analysis") -> bool:
    """判断当前是否可用 LLM(存在可用 provider 且未超预算)。

    :returns: 可用返回 ``True``。
    """
    if not provs.active_providers(capability):
        return False
    budget = _daily_budget()
    spend = _today_spend()
    if budget > 0 and spend >= budget:
        logger.warning("LLM 已达每日预算上限,降级为纯规则模式。")
        return False
    return True


def _budget_file() -> Path:
    """返回记录每日花费的 JSON 文件路径。"""
    return storage_root() / "llm_budget.json"


class BudgetAccountingError(ValueError):
    """预算账本不可用时停止后续收费请求，保留原始故障。"""


class _BudgetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, hide_input_in_errors=True)
    day: date = Field(alias="date")
    spend_usd: float = Field(ge=0)


def _read_spend(path: Path, today: date) -> float:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0.0
    try:
        record = _BudgetRecord.model_validate_json(text)
    except ValidationError as exc:
        raise BudgetAccountingError("预算记录无效，停止模型调用；请核查并恢复账本") from exc
    return record.spend_usd if record.day == today else 0.0


def _today_spend() -> float:
    """读取今日累计花费(美元)。

    :returns: 今日花费;无记录返回 0。
    """
    path = _budget_file()
    try:
        with FileLock(str(path) + ".lock", timeout=10):
            return _read_spend(path, date.today())
    except (OSError, Timeout, UnicodeError) as exc:
        raise BudgetAccountingError("预算记录不可读，停止模型调用") from exc


def _persist_budget(path: Path, record: _BudgetRecord) -> None:
    """持锁写入单次增量，仅有限重试 Windows 原子替换的占用错误。"""
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(record.model_dump_json(by_alias=True))
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(7):
            try:
                os.replace(temporary, path)
                return
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 6:
                    raise
                time.sleep(min(0.05 * 2**attempt, 0.5))
    finally:
        temporary.unlink(missing_ok=True)


def _add_spend(usd: float) -> None:
    """累加今日花费并持久化。

    :param usd: 本次花费(美元)。
    """
    if not math.isfinite(usd) or usd < 0:
        raise BudgetAccountingError("预算记录增量必须为有限非负数")
    path = _budget_file()
    try:
        with FileLock(str(path) + ".lock", timeout=10):
            today = date.today()
            record = _BudgetRecord(date=today, spend_usd=_read_spend(path, today) + usd)
            _persist_budget(path, record)
    except (OSError, Timeout, UnicodeError, ValidationError) as exc:
        raise BudgetAccountingError("预算记录写入失败，停止模型调用") from exc


# 模块级缓存:按 provider id 缓存 OpenAI 客户端实例,避免连接池泄漏。
# 长时间录制下(如4小时240个片段),每次创建新客户端会耗尽文件描述符。
# 缓存键仅包含非敏感连接身份；复用前核对 SDK 客户端已有的完整凭据。
_client_cache: dict[tuple[str, str], OpenAI] = {}
_client_cache_lock = threading.RLock()
_client_active: dict[int, int] = {}
_retired_clients: dict[int, object] = {}


class EmptyLLMResponseError(RuntimeError):
    """服务请求成功但没有返回可供业务使用的最终正文。"""


def _get_client(provider: provs.LLMProvider) -> OpenAI:
    """为指定 provider 创建或复用 OpenAI 兼容客户端(模块级单例缓存)。

    :param provider: 目标服务商配置。
    :returns: ``openai.OpenAI`` 实例。
    :raises RuntimeError: 未安装 openai 时。
    """
    with _client_cache_lock:
        return _create_or_reuse_client(provider)


def _create_or_reuse_client(provider: provs.LLMProvider) -> OpenAI:
    """在缓存锁内核对已有客户端凭据，不生成或缓存密钥摘要。"""
    connection_id = provider.connection_id or provider.id
    cache_key = (connection_id, provider.base_url)
    api_key = provider.api_key or "not-required"
    cached = _client_cache.get(cache_key)
    if cached is not None and hmac.compare_digest(cached.api_key.encode("utf-8"), api_key.encode("utf-8")):
        return cached
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            '未安装 openai SDK。源码安装请执行: pip install -e ".[llm]";'
            "Portable 请重新运行 Launcher 完成依赖修复,或升级到最新完整包。"
        ) from exc
    client = OpenAI(api_key=api_key, base_url=provider.base_url or None, max_retries=0)
    for key in list(_client_cache):
        if key[0] == connection_id:
            old = _client_cache.pop(key)
            if _client_active.get(id(old), 0):
                _retired_clients[id(old)] = old
            else:
                _close_client(old)
    _client_cache[cache_key] = client
    return client


def _close_client(client: object) -> None:
    """关闭已无活动请求的退役客户端。"""
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _account_usage(provider: provs.LLMProvider, resp: object) -> None:
    """按 provider 的 token 价格累加预算花费(价格为 0 则不计费)。

    :param provider: 服务商配置。
    :param resp: chat.completions 响应对象。
    """
    if provider.price_input_per_m <= 0 and provider.price_output_per_m <= 0:
        return
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    cost = (
        prompt_tokens / 1_000_000 * provider.price_input_per_m
        + completion_tokens / 1_000_000 * provider.price_output_per_m
    )
    _add_spend(cost)


def _field(value: object, name: str) -> object | None:
    """兼容对象属性和字典键读取 OpenAI 兼容响应字段。

    :param value: 响应对象或字典。
    :param name: 字段名。
    :returns: 字段值；不存在时返回 ``None``。
    """
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _first_choice(resp: object) -> object | None:
    """返回 chat.completions 的首个 choice。"""
    choices = _field(resp, "choices")
    if not choices:
        return None
    try:
        return choices[0]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        return None


def _content_text(value: object | None) -> str:
    """把字符串或 OpenAI 内容分块整理为纯文本。"""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, (list, tuple)):
        return ""
    parts: list[str] = []
    for part in value:
        if isinstance(part, str):
            text = part
        else:
            raw_text = _field(part, "text")
            text = raw_text if isinstance(raw_text, str) else ""
        if text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _extract_text(resp: object) -> str:
    """从 chat.completions 响应中取出首条消息的最终正文。

    ``reasoning_content`` 是模型的推理过程，不得作为业务正文写入转写、
    文案或 JSON 结果。

    :param resp: 响应对象。
    :returns: 文本内容(可能为空串)。
    """
    choice = _first_choice(resp)
    if choice is None:
        return ""
    message = _field(choice, "message")
    return _content_text(_field(message, "content")) if message is not None else ""


def _empty_response_details(resp: object) -> tuple[str, bool, int, int]:
    """提取空正文诊断信息，但不记录推理过程本身。

    :returns: ``(finish_reason, has_reasoning, completion_tokens, reasoning_tokens)``。
    """
    choice = _first_choice(resp)
    finish_value = _field(choice, "finish_reason") if choice is not None else None
    finish_reason = str(finish_value or "unknown")
    message = _field(choice, "message") if choice is not None else None
    reasoning = _content_text(_field(message, "reasoning_content")) if message is not None else ""
    usage = _field(resp, "usage")
    completion_value = _field(usage, "completion_tokens") if usage is not None else None
    details = _field(usage, "completion_tokens_details") if usage is not None else None
    reasoning_value = _field(details, "reasoning_tokens") if details is not None else None
    completion_tokens = int(completion_value or 0)
    reasoning_tokens = int(reasoning_value or 0)
    return finish_reason, bool(reasoning), completion_tokens, reasoning_tokens


def _low_reasoning_options(provider: provs.LLMProvider) -> dict[str, object]:
    """仅对已核实的官方端点和模型发送对应的思考控制参数。"""
    host = urlsplit(provider.base_url).hostname
    model = provider.model.strip().lower()
    if host == "api.deepseek.com" and model in {"deepseek-flash", "deepseek-v4-pro"}:
        return {"thinking": {"type": "disabled"}}
    if host == "api.minimax.cn" and model == "minimax-m3.1-flash":
        return {"reasoning_effort": "low"}
    return {}


def probe_provider(provider: provs.LLMProvider) -> str:
    """用包含思考开销的有限生成预算测试最终正文，最多调用两次。"""
    spend = _today_spend()
    budget = _daily_budget()
    if budget > 0 and spend >= budget:
        raise BudgetAccountingError("已达到每日模型预算，无法发起测试请求")
    return _complete(provider, "只回复 pong", max_tokens=4096, extra_body=_low_reasoning_options(provider) or None)


def _create_completion(
    client: object,
    provider: provs.LLMProvider,
    prompt: str,
    max_tokens: int,
    extra_body: dict[str, object] | None,
) -> object:
    """发起一次 OpenAI Chat Completions 请求。"""
    if provider.authorize is not None:
        provider.authorize()
    return client.chat.completions.create(  # type: ignore[attr-defined,no-any-return]
        model=provider.model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        timeout=settings.llm_request_timeout_s,
        **({"extra_body": extra_body} if extra_body else {}),
    )


def _complete(
    provider: provs.LLMProvider,
    prompt: str,
    max_tokens: int,
    extra_body: dict[str, object] | None = None,
) -> str:
    """用单个 provider 完成一次对话补全(失败会抛异常)。

    :param provider: 服务商配置。
    :param prompt: 用户提示词。
    :param max_tokens: 最大输出 token 数。
    :param extra_body: 额外请求体(如联网搜索开关)。
    :returns: 模型输出文本。
    """
    with _client_cache_lock:
        client = _get_client(provider)
        _client_active[id(client)] = _client_active.get(id(client), 0) + 1
    try:
        return _complete_using_client(client, provider, prompt, max_tokens, extra_body)
    finally:
        retired = None
        with _client_cache_lock:
            count = _client_active[id(client)] - 1
            if count:
                _client_active[id(client)] = count
            else:
                _client_active.pop(id(client), None)
                retired = _retired_clients.pop(id(client), None)
        if retired is not None:
            _close_client(retired)


def _complete_using_client(
    client: object, provider: provs.LLMProvider, prompt: str, max_tokens: int, extra_body: dict[str, object] | None
) -> str:
    """在客户端租用期间完成正文提取和空正文重试。"""
    if provider.max_output_tokens is not None:
        max_tokens = min(max_tokens, provider.max_output_tokens)
    resp = _create_completion(client, provider, prompt, max_tokens, extra_body)
    _account_usage(provider, resp)
    text = _extract_text(resp)
    if text:
        return text

    finish_reason, has_reasoning, completion_tokens, reasoning_tokens = _empty_response_details(resp)
    if finish_reason in {"content_filter", "tool_calls"}:
        raise EmptyLLMResponseError(f"服务未返回最终正文: finish_reason={finish_reason}, reasoning={has_reasoning}")

    retry_extra = dict(extra_body or {})
    retry_mode = "原参数"
    retry_extra.update(_low_reasoning_options(provider))
    if retry_extra != (extra_body or {}):
        retry_mode = "使用已核实的低思考参数"
    elif finish_reason == "length":
        raise EmptyLLMResponseError(
            f"输出预算已耗尽且未返回最终正文: max_tokens={max_tokens}, "
            f"finish_reason={finish_reason}, completion_tokens={completion_tokens}, "
            f"reasoning_tokens={reasoning_tokens}, reasoning={has_reasoning}；"
            "请提高该任务的输出预算，或选择思考开销较低的模型。"
        )
    logger.warning(
        "模型 {} 返回空正文,finish_reason={} completion_tokens={} reasoning_tokens={} reasoning={};将{}重试一次。",
        provider.name,
        finish_reason,
        completion_tokens,
        reasoning_tokens,
        has_reasoning,
        retry_mode,
    )

    retry_resp = _create_completion(client, provider, prompt, max_tokens, retry_extra or None)
    _account_usage(provider, retry_resp)
    retry_text = _extract_text(retry_resp)
    if retry_text:
        return retry_text

    retry_finish, retry_reasoning, retry_tokens, retry_reasoning_tokens = _empty_response_details(retry_resp)
    raise EmptyLLMResponseError(
        "服务连续两次返回空正文: "
        f"finish_reason={retry_finish}, completion_tokens={retry_tokens}, "
        f"reasoning_tokens={retry_reasoning_tokens}, reasoning={retry_reasoning}"
    )


def call_text(prompt: str, max_tokens: int = 512, *, capability: TextCapability = "analysis") -> str | None:
    """按优先级遍历多个大模型完成文本生成,失败自动降级到下一个。

    任何 provider 成功返回非空文本即采用;全部不可用/失败返回 ``None``,
    由调用方决定回退策略。

    :param prompt: 用户提示词。
    :param max_tokens: 最大输出 token 数。
    :returns: 模型输出文本;不可用或全部失败时返回 ``None``。
    """
    if not is_llm_enabled(capability):
        return None
    for provider in provs.active_providers(capability):
        try:
            text = _complete(provider, prompt, max_tokens)
            if text:
                return text
            logger.warning("模型 {} 返回空结果,尝试下一个。", provider.name)
        except BudgetAccountingError:
            raise
        except Exception as exc:  # noqa: BLE001 — 逐个降级
            logger.opt(exception=exc).warning(
                "模型调用失败 provider={} model={}，尝试下一个: {}",
                provider.id,
                provider.model,
                safe_exception_summary(exc),
            )
    logger.error("所有大模型均不可用,降级为纯规则模式。")
    return None


def call_web_search(
    prompt: str,
    max_tokens: int = 2048,
    *,
    capability: TextCapability = "trends",
) -> str | None:
    """按优先级遍历多个大模型完成(尽力联网搜索的)文本生成,失败自动降级。

    每个 provider 若配置了联网搜索开关键(如 ``enable_search``),先带该参数尝试;
    该服务商不支持则对同一 provider 回退为普通调用;仍失败则降级到下一个 provider。

    :param prompt: 用户提示词。
    :param max_tokens: 最大输出 token 数。
    :returns: 模型输出文本;全部不可用时返回 ``None``。
    """
    if not is_llm_enabled(capability):
        return None
    for provider in provs.active_providers(capability):
        search_param = provider.web_search_param.strip()
        # 1) 带联网搜索参数尝试。
        if search_param:
            try:
                text = _complete(provider, prompt, max_tokens, {search_param: True})
                if text:
                    return text
            except BudgetAccountingError:
                raise
            except Exception as exc:  # noqa: BLE001 — 不支持该参数则退化为普通调用
                logger.opt(exception=exc).warning(
                    "模型 provider={} model={} 带联网参数({})调用失败,尝试普通调用: {}",
                    provider.id,
                    provider.model,
                    search_param,
                    safe_exception_summary(exc),
                )
        # 2) 普通调用(无联网)。
        try:
            text = _complete(provider, prompt, max_tokens)
            if text:
                return text
        except BudgetAccountingError:
            raise
        except Exception as exc:  # noqa: BLE001 — 降级到下一个 provider
            logger.opt(exception=exc).warning(
                "模型调用失败 provider={} model={}，尝试下一个: {}",
                provider.id,
                provider.model,
                safe_exception_summary(exc),
            )
    logger.error("所有大模型均不可用,网感采集本次跳过。")
    return None


def call_trend_search(
    prompt: str,
    max_tokens: int = 2048,
) -> str | None:
    """按热点路由生成内容；旧专用配置由兼容适配器读取，全部受预算限制。"""
    return call_web_search(prompt, max_tokens, capability="trends")


def extract_json_array(raw: str) -> list[object] | None:
    """从模型输出中鲁棒地抽取首个 JSON 数组。

    :param raw: 模型原始输出。
    :returns: 解析出的列表;失败返回 ``None``。
    """
    text = raw.strip()
    if "```" in text:
        text = text.replace("```json", "").replace("```", "").strip()
    start = text.find("[")
    if start == -1:
        return None
    end = text.rfind("]")
    if end > start:
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
        else:
            return data if isinstance(data, list) else None

    # 模型可能因 token 上限在数组尾部被截断。此处只保留已经完整闭合的元素，
    # 绝不猜测或修补半个对象，避免把不完整热点写入资料库。
    decoder = json.JSONDecoder()
    items: list[object] = []
    cursor = start + 1
    while cursor < len(text):
        while cursor < len(text) and (text[cursor].isspace() or text[cursor] == ","):
            cursor += 1
        if cursor >= len(text) or text[cursor] == "]":
            break
        try:
            item, cursor = decoder.raw_decode(text, cursor)
        except json.JSONDecodeError:
            break
        items.append(item)
    return items or None


def extract_json(raw: str) -> dict | None:
    """从模型输出中鲁棒地抽取首个 JSON 对象。

    容错处理:剥离 ```json 代码围栏,定位首尾花括号。

    :param raw: 模型原始输出。
    :returns: 解析出的字典;失败返回 ``None``。
    """
    text = raw.strip()
    if "```" in text:
        text = text.replace("```json", "").replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


_TRANSCRIPT_REFINEMENT_PROMPT = """你是一名中文直播内容编辑。请整理下面这一段自动语音识别文本。

要求：
1. 不删减事实、专有名词、数字和关键语气，不添加原文没有的信息；
2. 删除 ASR 解码或 VAD 分句边界产生的连续重复词句，但保留主播有明确语义的强调、复述和口头禅；
3. 仅在上下文足够明确时修正常见同音字或断句错误；
4. 补全标点，按语义整理成可读句子和短段落；
5. 再给出一段不超过 120 个汉字的内容概括；
6. 只输出 JSON，不要代码围栏或其他说明。

输出格式：
{{"clean_text":"整理后的完整文本","summary":"本片段概括"}}

原始转写：
{text}"""


def refine_transcript(raw_text: str) -> TranscriptRefinement | None:
    """用 LLM 整理并概括一个切片的 ASR 文本。

    LLM 不可用、输出不完整或解析失败时返回 ``None``，调用方应保留原始文本。

    :param raw_text: 已完成房间别名替换的原始 ASR 文本。
    :returns: 可读文本及摘要；无法可靠整理时返回 ``None``。
    """
    source = raw_text.strip()
    if not source:
        return None
    raw = call_text(
        _TRANSCRIPT_REFINEMENT_PROMPT.format(text=source),
        max_tokens=settings.transcript_llm_refine_max_tokens,
        capability="transcript",
    )
    if raw is None:
        return None
    data = extract_json(raw)
    if data is None:
        logger.warning("转写整理输出无法解析为 JSON: head={!r} tail={!r}", raw[:160], raw[-160:])
        return None
    clean_text = str(data.get("clean_text", "")).strip()
    summary = str(data.get("summary", "")).strip()
    if not clean_text or not summary:
        logger.warning("转写整理输出缺少 clean_text 或 summary，已保留原始 ASR 文本。")
        return None
    return TranscriptRefinement(clean_text=clean_text, summary=summary[:120])


_JUDGE_PROMPT = """你是一名 Bilibili 短视频切片编辑。下面是一段直播录制候选窗口的信息,请判断它是否包含\
值得单独切片传播的"高光/爆点",并给出建议的切片时间范围。start_offset/end_offset 必须相对下面\
这个候选窗口的起点填写；系统会再换算到原始录制分段。start_offset 是视频开始点，end_offset 必须\
晚于 reason 所描述事件的完整结束时刻，绝不能填写事件开始时刻或早于 start_offset。候选窗口在原始分段中的\
起点是 {window_start:.3f} 秒。

转写文本:
{text}

规则特征(0-1,越高越可能是爆点):
{features}

弹幕摘要:
{danmaku}

请只输出 JSON,不要任何额外文字；reason 不超过 30 个汉字且不得换行。格式:
{{"is_highlight": true/false, "score": 0~1, "reason": "简短中文理由", \
"start_offset": 数字或null, "end_offset": 数字或null}}"""


def _recover_highlight_json(raw: str) -> dict[str, object] | None:
    """从被截断的高光 JSON 中恢复已经完整输出的核心字段。

    只有 ``is_highlight`` 和 ``score`` 都完整存在时才返回结果；理由和偏移量
    属于可选字段，避免截断理由导致已生成的评分被整体丢弃。

    :param raw: LLM 原始输出。
    :returns: 可供高光判断使用的字段字典，无法可靠恢复时返回 ``None``。
    """
    flag_match = re.search(r'"is_highlight"\s*:\s*(true|false)', raw, flags=re.IGNORECASE)
    score_match = re.search(r'"score"\s*:\s*(-?\d+(?:\.\d+)?)', raw)
    if flag_match is None or score_match is None:
        return None
    data: dict[str, object] = {
        "is_highlight": flag_match.group(1).lower() == "true",
        "score": float(score_match.group(1)),
    }
    reason_match = re.search(r'"reason"\s*:\s*"([^"\r\n]*)', raw)
    if reason_match is not None:
        data["reason"] = reason_match.group(1).strip()[:30]
    for key in ("start_offset", "end_offset"):
        match = re.search(rf'"{key}"\s*:\s*(null|-?\d+(?:\.\d+)?)', raw, flags=re.IGNORECASE)
        if match is not None:
            data[key] = None if match.group(1).lower() == "null" else float(match.group(1))
    return data


def judge_highlight(
    text: str,
    features: dict[str, float],
    danmaku_summary: str = "",
    window_start_offset_s: float = 0.0,
) -> HighlightJudgement | None:
    """调用 LLM 复核某片段是否为高光。

    :param text: 转写文本。
    :param features: 规则特征字典(维度名->0-1 分)。
    :param danmaku_summary: 弹幕摘要(可空)。
    :param window_start_offset_s: 当前文本窗口相对原始录制分段的起点秒数。
    :returns: :class:`HighlightJudgement`;LLM 不可用或出错时返回 ``None``。
    """
    window_start = float(window_start_offset_s)
    if not math.isfinite(window_start):
        window_start = 0.0
    prompt = _JUDGE_PROMPT.format(
        text=text or "(无转写)",
        features=json.dumps(features, ensure_ascii=False),
        danmaku=danmaku_summary or "(无弹幕数据)",
        window_start=window_start,
    )
    raw = call_text(prompt, max_tokens=settings.highlight_llm_max_tokens)
    if raw is None:
        return None
    data = extract_json(raw)
    if data is None:
        data = _recover_highlight_json(raw)
        if data is None:
            logger.warning("LLM 复核输出无法解析为 JSON: head={!r} tail={!r}", raw[:160], raw[-160:])
            return None
        logger.warning("LLM 复核 JSON 被截断，已恢复完整的核心判断字段。")

    score = _opt_float(data.get("score"))
    score = max(0.0, min(1.0, score if score is not None else 0.0))
    raw_flag = data.get("is_highlight", False)
    is_highlight = raw_flag if isinstance(raw_flag, bool) else str(raw_flag).lower() == "true"

    suggested_start = _opt_float(data.get("start_offset"))
    suggested_end = _opt_float(data.get("end_offset"))
    return HighlightJudgement(
        is_highlight=is_highlight,
        score=score,
        reason=str(data.get("reason", "")).replace("\n", " ").strip()[:30],
        suggested_start_offset=window_start + suggested_start if suggested_start is not None else None,
        suggested_end_offset=window_start + suggested_end if suggested_end is not None else None,
    )


def _opt_float(value: object) -> float | None:
    """把可空数值安全转换为 ``float | None``。

    :param value: 原始值。
    :returns: 浮点数或 ``None``。
    """
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
