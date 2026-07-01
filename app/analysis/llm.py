"""大模型(OpenAI 兼容协议)高光复核与文案能力的底层封装。

系统主要在中国大陆境内运行,Anthropic/Cursor 系模型连接不稳定,故 LLM 层采用
**OpenAI 兼容协议**,可对接境内可稳定访问的服务商(DeepSeek / 通义千问 Qwen /
Moonshot Kimi / 智谱 GLM 等)——只需在 ``.env`` 配置 ``LLM_BASE_URL`` /
``LLM_API_KEY`` / ``LLM_MODEL``。语音转写仍由本地 Whisper 完成,不依赖联网大模型。

设计目标(对应"降低 AI 成本""优先稳定"):

* **可禁用**:未配置 API Key 时自动跳过 LLM,走纯规则,不报错;
* **预算护栏**:可设每日花费上限(按可配置的 token 价格估算),超额自动降级;
* **失败回退**:任何异常都返回 ``None``,由上层用规则分兜底;
* **可选依赖**:``openai`` 属可选包(``pip install -e ".[llm]"``)。

本模块只负责"判断是否高光"。文案生成在 ``publishing/copywriter`` 复用此处的客户端。
"""

from __future__ import annotations

import json
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


def _get_client(provider: provs.LLMProvider):  # noqa: ANN202 — 返回 openai.OpenAI
    """为指定 provider 创建 OpenAI 兼容客户端。

    :param provider: 目标服务商配置。
    :returns: ``openai.OpenAI`` 实例。
    :raises RuntimeError: 未安装 openai 时。
    """
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError('未安装 openai。请执行: pip install -e ".[llm]"。') from exc
    return OpenAI(api_key=provider.api_key, base_url=provider.base_url or None)


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


def _extract_text(resp: object) -> str:
    """从 chat.completions 响应中取出首条消息文本。

    :param resp: 响应对象。
    :returns: 文本内容(可能为空串)。
    """
    try:
        return resp.choices[0].message.content or ""  # type: ignore[attr-defined,index]
    except (AttributeError, IndexError, TypeError):
        return ""


def _complete(
    provider: provs.LLMProvider,
    prompt: str,
    max_tokens: int,
    extra_body: dict | None = None,
) -> str:
    """用单个 provider 完成一次对话补全(失败会抛异常)。

    :param provider: 服务商配置。
    :param prompt: 用户提示词。
    :param max_tokens: 最大输出 token 数。
    :param extra_body: 额外请求体(如联网搜索开关)。
    :returns: 模型输出文本。
    """
    client = _get_client(provider)
    resp = client.chat.completions.create(
        model=provider.model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        **({"extra_body": extra_body} if extra_body else {}),
    )
    _account_usage(provider, resp)
    return _extract_text(resp)


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
                    provider.name, search_param, exc,
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


def extract_json_array(raw: str) -> list | None:
    """从模型输出中鲁棒地抽取首个 JSON 数组。

    :param raw: 模型原始输出。
    :returns: 解析出的列表;失败返回 ``None``。
    """
    text = raw.strip()
    if "```" in text:
        text = text.replace("```json", "").replace("```", "").strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


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


_JUDGE_PROMPT = """你是一名 Bilibili 短视频切片编辑。下面是一段直播录制片段的信息,请判断它是否包含\
值得单独切片传播的"高光/爆点",并给出建议的切片时间范围(相对本片段起点的秒数)。

转写文本:
{text}

规则特征(0-1,越高越可能是爆点):
{features}

弹幕摘要:
{danmaku}

请只输出 JSON,不要任何额外文字,格式:
{{"is_highlight": true/false, "score": 0~1, "reason": "简短中文理由", \
"start_offset": 数字或null, "end_offset": 数字或null}}"""


def judge_highlight(
    text: str,
    features: dict[str, float],
    danmaku_summary: str = "",
) -> HighlightJudgement | None:
    """调用 Claude 复核某片段是否为高光。

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
        logger.warning("LLM 复核输出无法解析为 JSON: {}", raw[:200])
        return None

    return HighlightJudgement(
        is_highlight=bool(data.get("is_highlight", False)),
        score=float(data.get("score", 0.0)),
        reason=str(data.get("reason", "")),
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
