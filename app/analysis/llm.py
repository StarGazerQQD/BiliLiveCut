"""大模型(OpenAI 兼容协议)高光复核与文案能力的底层封装。

    系统主要在中国大陆境内运行,故 LLM 层采用
    **OpenAI 兼容协议**,可对接境内可稳定访问的服务商(DeepSeek / 通义千问 Qwen /
Moonshot Kimi / 智谱 GLM 等)——只需在 ``.env`` 配置 ``LLM_BASE_URL`` /
``LLM_API_KEY`` / ``LLM_MODEL``。语音转写仍由本地 Whisper 完成,不依赖联网大模型。

设计目标(对应"降低 AI 成本""优先稳定"):

* **可禁用**:未配置 API Key 时自动跳过 LLM,走纯规则,不报错;
* **预算护栏**:可设每日花费上限(按可配置的 token 价格估算),超额自动降级;
* **失败回退**:任何异常都返回 ``None``,由上层用规则分兜底;
* **依赖边界**:Portable 已内置 ``openai`` SDK;源码安装可通过
  ``pip install -e ".[llm]"`` 启用。

本模块只负责"判断是否高光"。文案生成在 ``publishing/copywriter`` 复用此处的客户端。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from loguru import logger

from app.analysis import llm_providers as provs
from app.core.config import settings
from app.core.paths import storage_root


def _daily_budget() -> float:
    """返回每日预算(优先 ``llm_daily_budget``,回退旧 ``llm_daily_budget_usd``)。

    :returns: 预算金额;0 表示不限额。
    """
    return settings.llm_daily_budget or settings.llm_daily_budget_usd


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


def is_llm_enabled() -> bool:
    """判断当前是否可用 LLM(存在可用 provider 且未超预算)。

    :returns: 可用返回 ``True``。
    """
    if not provs.active_providers():
        return False
    budget = _daily_budget()
    if budget > 0 and _today_spend() >= budget:
        logger.warning("LLM 已达每日预算上限,降级为纯规则模式。")
        return False
    return True


def _budget_file() -> Path:
    """返回记录每日花费的 JSON 文件路径。"""
    return storage_root() / "llm_budget.json"


def _today_spend() -> float:
    """读取今日累计花费(美元)。

    :returns: 今日花费;无记录返回 0。
    """
    path = _budget_file()
    if not path.exists():
        return 0.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0.0
    if data.get("date") != date.today().isoformat():
        return 0.0
    return float(data.get("spend_usd", 0.0))


def _add_spend(usd: float) -> None:
    """累加今日花费并持久化。

    :param usd: 本次花费(美元)。
    """
    today = date.today().isoformat()
    current = _today_spend()
    try:
        _budget_file().write_text(
            json.dumps({"date": today, "spend_usd": current + usd}),
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover
        logger.warning("写入预算文件失败: {}", exc)


# 模块级缓存:按 provider id 缓存 OpenAI 客户端实例,避免连接池泄漏。
# 长时间录制下(如4小时240个片段),每次创建新客户端会耗尽文件描述符。
# 注意:缓存无 TTL 过期,provider 配置变更需重启应用生效。
_client_cache: dict[str, object] = {}


class EmptyLLMResponseError(RuntimeError):
    """服务请求成功但没有返回可供业务使用的最终正文。"""


def _get_client(provider: provs.LLMProvider):  # noqa: ANN202 — 返回 openai.OpenAI
    """为指定 provider 创建或复用 OpenAI 兼容客户端(模块级单例缓存)。

    :param provider: 目标服务商配置。
    :returns: ``openai.OpenAI`` 实例。
    :raises RuntimeError: 未安装 openai 时。
    """
    cache_key = f"{provider.id}:{provider.base_url}:{provider.api_key[:8]}"
    if cache_key in _client_cache:
        return _client_cache[cache_key]
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            '未安装 openai SDK。源码安装请执行: pip install -e ".[llm]";'
            "Portable 请重新运行 Launcher 完成依赖修复,或升级到最新完整包。"
        ) from exc
    client = OpenAI(api_key=provider.api_key, base_url=provider.base_url or None)
    _client_cache[cache_key] = client
    return client


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


def _should_disable_thinking(provider: provs.LLMProvider, has_reasoning: bool) -> bool:
    """判断空正文重试是否应关闭 DeepSeek 思考模式。"""
    if has_reasoning:
        return True
    return "deepseek-v4" in provider.model.lower()


def _create_completion(
    client: object,
    provider: provs.LLMProvider,
    prompt: str,
    max_tokens: int,
    extra_body: dict[str, object] | None,
) -> object:
    """发起一次 OpenAI Chat Completions 请求。"""
    return client.chat.completions.create(  # type: ignore[attr-defined,no-any-return]
        model=provider.model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
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
    client = _get_client(provider)
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
    if _should_disable_thinking(provider, has_reasoning):
        retry_extra["thinking"] = {"type": "disabled"}
        retry_mode = "关闭思考模式"
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


def call_text(prompt: str, max_tokens: int = 512) -> str | None:
    """按优先级遍历多个大模型完成文本生成,失败自动降级到下一个。

    任何 provider 成功返回非空文本即采用;全部不可用/失败返回 ``None``,
    由调用方决定回退策略。

    :param prompt: 用户提示词。
    :param max_tokens: 最大输出 token 数。
    :returns: 模型输出文本;不可用或全部失败时返回 ``None``。
    """
    if not is_llm_enabled():
        return None
    for provider in provs.active_providers():
        try:
            text = _complete(provider, prompt, max_tokens)
            if text:
                return text
            logger.warning("模型 {} 返回空结果,尝试下一个。", provider.name)
        except Exception as exc:  # noqa: BLE001 — 逐个降级
            logger.warning("模型 {} 调用失败,降级下一个: {}", provider.name, exc)
    logger.error("所有大模型均不可用,降级为纯规则模式。")
    return None


def call_web_search(
    prompt: str,
    max_tokens: int = 2048,
    max_searches: int = 5,  # noqa: ARG001 — 兼容旧签名;OpenAI 兼容协议按服务商内部控制
    model: str = "",  # noqa: ARG001 — 兼容旧签名;实际模型由各 provider 决定
) -> str | None:
    """按优先级遍历多个大模型完成(尽力联网搜索的)文本生成,失败自动降级。

    每个 provider 若配置了联网搜索开关键(如 ``enable_search``),先带该参数尝试;
    该服务商不支持则对同一 provider 回退为普通调用;仍失败则降级到下一个 provider。

    :param prompt: 用户提示词。
    :param max_tokens: 最大输出 token 数。
    :param max_searches: 兼容旧签名的占位参数。
    :param model: 兼容旧签名的占位参数。
    :returns: 模型输出文本;全部不可用时返回 ``None``。
    """
    if not is_llm_enabled():
        return None
    for provider in provs.active_providers():
        search_param = provider.web_search_param.strip()
        # 1) 带联网搜索参数尝试。
        if search_param:
            try:
                text = _complete(provider, prompt, max_tokens, {search_param: True})
                if text:
                    return text
            except Exception as exc:  # noqa: BLE001 — 不支持该参数则退化为普通调用
                logger.warning(
                    "模型 {} 联网搜索参数({})不被支持,改普通调用: {}",
                    provider.name,
                    search_param,
                    exc,
                )
        # 2) 普通调用(无联网)。
        try:
            text = _complete(provider, prompt, max_tokens)
            if text:
                return text
        except Exception as exc:  # noqa: BLE001 — 降级到下一个 provider
            logger.warning("模型 {} 调用失败,降级下一个: {}", provider.name, exc)
    logger.error("所有大模型均不可用,网感采集本次跳过。")
    return None


def call_trend_search(
    prompt: str,
    max_tokens: int = 2048,
    max_searches: int = 5,
) -> str | None:
    """趋势采集专用:使用 ``TREND_API_KEY`` / ``TREND_BASE_URL`` / ``TREND_MODEL`` 的独立配置。

    若未配置趋势专用 API 则回退到通用 LLM 的多模型列表。

    用于网感资料库的联网采集,可与通用 LLM 使用不同的模型/服务商。

    :param prompt: 用户提示词。
    :param max_tokens: 最大输出 token 数。
    :param max_searches: 最大搜索次数(兼容占位,实际取决于服务商)。
    :returns: 模型输出文本;全部不可用时返回 ``None``。
    """
    from app.analysis.llm_providers import LLMProvider

    # 是否配置了趋势专用 API
    trend_key = settings.trend_api_key.strip()
    trend_url = settings.trend_base_url.strip()

    if trend_key and trend_url:
        from app.core.config import settings as cfg

        trend_provider = LLMProvider(
            id="trend",
            name="网感采集专用",
            base_url=trend_url,
            api_key=trend_key,
            model=settings.trend_model or cfg.llm_model or "deepseek-chat",
            web_search_param=cfg.llm_web_search_param,
        )
        search_param = trend_provider.web_search_param.strip()
        # 1) 带联网搜索参数尝试。
        if search_param:
            try:
                text = _complete(trend_provider, prompt, max_tokens, {search_param: True})
                if text:
                    logger.info("趋势采集(专用 API + 联网搜索)成功,model={}", trend_provider.model)
                    return text
            except Exception as exc:
                logger.warning(
                    "趋势采集专用 API 联网搜索失败({}),改普通调用: {}",
                    trend_provider.model,
                    exc,
                )
        # 2) 普通调用(无联网)。
        try:
            text = _complete(trend_provider, prompt, max_tokens)
            if text:
                logger.info("趋势采集(专用 API 普通调用)成功,model={}", trend_provider.model)
                return text
        except Exception as exc:
            logger.warning("趋势采集专用 API 调用失败({}),回退通用 LLM: {}", trend_provider.model, exc)

    # 回退:使用通用 LLM 多模型列表(含联网搜索)
    logger.info("趋势采集未配置专用 API,回退到通用 LLM。")
    return call_web_search(prompt, max_tokens, max_searches)


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
2. 仅在上下文足够明确时修正常见同音字或断句错误；
3. 补全标点，按语义整理成可读句子和短段落；
4. 再给出一段不超过 120 个汉字的内容概括；
5. 只输出 JSON，不要代码围栏或其他说明。

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


_JUDGE_PROMPT = """你是一名 Bilibili 短视频切片编辑。下面是一段直播录制片段的信息,请判断它是否包含\
值得单独切片传播的"高光/爆点",并给出建议的切片时间范围(相对本片段起点的秒数)。

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
) -> HighlightJudgement | None:
    """调用 LLM 复核某片段是否为高光。

    :param text: 转写文本。
    :param features: 规则特征字典(维度名->0-1 分)。
    :param danmaku_summary: 弹幕摘要(可空)。
    :returns: :class:`HighlightJudgement`;LLM 不可用或出错时返回 ``None``。
    """
    prompt = _JUDGE_PROMPT.format(
        text=text or "(无转写)",
        features=json.dumps(features, ensure_ascii=False),
        danmaku=danmaku_summary or "(无弹幕数据)",
    )
    raw = call_text(prompt, max_tokens=512)
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

    return HighlightJudgement(
        is_highlight=is_highlight,
        score=score,
        reason=str(data.get("reason", "")).replace("\n", " ").strip()[:30],
        suggested_start_offset=_opt_float(data.get("start_offset")),
        suggested_end_offset=_opt_float(data.get("end_offset")),
    )


def _opt_float(value: object) -> float | None:
    """把可空数值安全转换为 ``float | None``。

    :param value: 原始值。
    :returns: 浮点数或 ``None``。
    """
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
