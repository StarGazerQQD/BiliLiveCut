# ruff: noqa: D100
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Protocol
from urllib.parse import urlsplit

from app.models.contracts import MAX_CONFIGURABLE_TOKENS, ModelCapacity

CAPACITY_POLICY_VERSION = "model-capacity-2026-10-29-v1"
UNKNOWN_OUTPUT_TOKENS = 65_536
DEFAULT_OUTPUT_TOKENS = 1_048_576
MODEL_CAPACITIES = (
    ModelCapacity(
        id="deepseek",
        base_urls=("https://api.deepseek.com", "https://api.deepseek.com/v1"),
        models=("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"),
        context_window_tokens=1_000_000,
        max_output_tokens=393_216,
        sources=(
            "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            "https://api-docs.deepseek.com/api/create-chat-completion/",
        ),
        note="上下文原文为 1M，按 1000000 保守取值；384K 输出包含思考。",
    ),
    ModelCapacity(
        id="bailian",
        base_urls=("https://dashscope.aliyuncs.com/compatible-mode/v1",),
        models=("qwen-plus", "qwen-plus-2025-12-01"),
        context_window_tokens=1_000_000,
        max_input_tokens=997_952,
        max_output_tokens=32_768,
        sources=("https://help.aliyun.com/zh/model-studio/qwen-plus",),
        note="当前默认非思考模式；32768 为回答上限。",
    ),
    ModelCapacity(
        id="volcengine",
        base_urls=("https://ark.cn-beijing.volces.com/api/v3",),
        models=("doubao-seed-2-1-pro-260915",),
        context_window_tokens=1_024_000,
        max_input_tokens=1_024_000,
        max_output_tokens=256_000,
        reasoning_reserve_tokens=256_000,
        sources=(
            "https://docs.volcengine.com/docs/ark/model-list?lang=zh",
            "https://docs.volcengine.com/docs/ark/context-management?lang=zh",
        ),
        note="型号表 1024k/256k 按十进制保守取值；回答外另预留思考额度，通用 API 页仍列旧限制。",
    ),
    ModelCapacity(
        id="moonshot",
        base_urls=("https://api.moonshot.cn/v1", "https://api.moonshot.ai/v1"),
        models=("kimi-k3",),
        context_window_tokens=1_048_576,
        max_output_tokens=1_048_576,
        output_parameter="max_completion_tokens",
        sources=("https://platform.kimi.com/docs/api/chat", "https://www.kimi.com/help/kimi-api/api-troubleshooting"),
        note="思考与回答共享输出额度；实际输出还受剩余上下文约束。",
    ),
    ModelCapacity(
        id="bigmodel",
        base_urls=("https://open.bigmodel.cn/api/paas/v4",),
        models=("glm-5.3",),
        context_window_tokens=1_000_000,
        max_output_tokens=131_072,
        sources=(
            "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3",
            "https://docs.bigmodel.cn/cn/guide/start/concept-param",
        ),
        note="上下文原文为 1M，按 1000000 保守取值；始终启用思考。",
    ),
    ModelCapacity(
        id="mimo",
        base_urls=("https://api.xiaomimimo.com/v1",),
        models=("mimo-v2.6-pro",),
        context_window_tokens=1_048_576,
        max_output_tokens=131_072,
        output_parameter="max_completion_tokens",
        sources=(
            "https://mimo.mi.com/docs/zh-CN/tokenplan/integration/opencode",
            "https://mimo.mi.com/docs/en-US/api/chat/openai-api",
        ),
        note="输出额度包含思考与回答。",
    ),
    ModelCapacity(
        id="siliconflow",
        base_urls=("https://api.siliconflow.cn/v1",),
        models=("deepseek-ai/DeepSeek-V3.2",),
        context_window_tokens=163_840,
        reasoning_reserve_tokens=32_768,
        sources=("https://www.siliconflow.cn/models", "https://docs.siliconflow.cn/docs/api/chat-completions-post"),
        note="独立回答上限未核实，留空沿用 65536 本地预算；可依据账户模型卡手动设置。",
    ),
    ModelCapacity(
        id="minimax",
        base_urls=("https://api.minimax.cn/v1", "https://api.minimax.io/v1"),
        models=("MiniMax-M3.1-Flash",),
        context_window_tokens=1_000_000,
        max_output_tokens=524_288,
        output_parameter="max_completion_tokens",
        sources=(
            "https://platform.minimax.cn/docs/api-reference/text-openai-api",
            "https://platform.minimax.cn/docs/api-reference/text-chat-openai",
        ),
        note="输出含思考；官方推荐 131072，允许最大 524288。",
    ),
    ModelCapacity(
        id="tokenhub",
        base_urls=("https://tokenhub.tencentmaas.com/v1",),
        models=("hy4-preview",),
        context_window_tokens=1_000_000,
        max_input_tokens=960_000,
        max_output_tokens=64_000,
        sources=("https://cloud.tencent.com/document/product/1823/130051",),
        note="官方 1M/960k/64k 未定义换算，按十进制保守取值。",
    ),
    ModelCapacity(
        id="spark",
        base_urls=("https://spark-api-open.xf-yun.com/v1",),
        models=("4.0Ultra",),
        max_input_tokens=32_000,
        max_output_tokens=32_768,
        sources=("https://www.xfyun.cn/doc/spark/HTTP调用文档.html",),
        note="当前为 X1.5 快思考；32K 输入按 32000 保守取值，共享上下文未公布。",
    ),
    ModelCapacity(
        id="lkeap-legacy",
        base_urls=("https://api.lkeap.cloud.tencent.com/v1",),
        models=("deepseek-r1-0528",),
        context_window_tokens=128_000,
        max_input_tokens=96_000,
        max_output_tokens=16_000,
        reasoning_reserve_tokens=32_000,
        sources=("https://cloud.tencent.com/document/product/1772/115969",),
        note="旧接口 k 按十进制保守取值；回答不含思考，另预留 32k；新配置请使用 TokenHub。",
    ),
    ModelCapacity(
        id="hunyuan-legacy",
        base_urls=("https://api.hunyuan.cloud.tencent.com/v1",),
        models=("hunyuan-turbos-latest",),
        sources=("https://cloud.tencent.com/document/product/1729/131925",),
        note="官方公告该型号已下线，旧平台已于 2026-09-30 停服；请迁移 TokenHub 模型及专属密钥。",
    ),
)


class CapacityRoute(Protocol):
    """容量计算只读取路由身份和限制，不访问凭据或数据库。"""

    base_url: str
    model: str
    context_window_tokens: int | None
    max_input_tokens: int | None
    max_output_tokens: int | None


class ModelCapacityError(ValueError):
    """完整请求按保守估算已超出所选模型或管理员容量限制。"""


def model_capacity(base_url: str, model: str) -> ModelCapacity | None:
    """仅匹配原厂 HTTPS 端点与精确型号，不向代理或近似域名继承能力。"""
    parts = urlsplit(base_url)
    if parts.scheme != "https" or parts.username or parts.password or parts.query or parts.fragment:
        return None
    if parts.port not in {None, 443}:
        return None
    endpoint = f"https://{parts.hostname}{parts.path.rstrip('/')}"
    return next((item for item in MODEL_CAPACITIES if endpoint in item.base_urls and model in item.models), None)


def capacity_identity(route: CapacityRoute) -> dict[str, object]:
    """让已保存结果随手动限制及已核实能力变化失效，不保存服务地址。"""
    profile = model_capacity(route.base_url, route.model)
    return {
        "policy": CAPACITY_POLICY_VERSION,
        "profile": profile.id if profile else None,
        "context": route.context_window_tokens,
        "input": route.max_input_tokens,
        "output": route.max_output_tokens,
    }


def _minimum(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def output_budget(
    route: CapacityRoute, requested: int, messages: Sequence[object], response_format: object = None
) -> int:
    """给全部消息和响应模式预留空间，按保守估算收缩输出，绝不截断证据。

    UTF-8 字节数加协议余量是本地保守估算，非供应商 tokenizer 的精确计数。
    服务端的实际 usage 和上下文校验仍为最终依据。
    """
    if type(requested) is not int or not 1 <= requested <= MAX_CONFIGURABLE_TOKENS:
        raise ValueError("模型输出 token 上限无效")
    profile = model_capacity(route.base_url, route.model)
    encoded = json.dumps([messages, response_format], ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    estimated_input = len(encoded.encode("utf-8")) + 512
    input_limit = _minimum(route.max_input_tokens, profile.max_input_tokens if profile else None)
    context_limit = _minimum(route.context_window_tokens, profile.context_window_tokens if profile else None)
    output_limit = _minimum(route.max_output_tokens, profile.max_output_tokens if profile else None)
    output = min(requested, output_limit if output_limit is not None else UNKNOWN_OUTPUT_TOKENS)
    if input_limit is not None and estimated_input > input_limit:
        raise ModelCapacityError(f"完整请求保守估算 {estimated_input} token，超过输入限制 {input_limit}；未发送请求。")
    if context_limit is not None:
        reserve = profile.reasoning_reserve_tokens if profile else 0
        remaining = context_limit - estimated_input - reserve
        if remaining < min(16, output):
            raise ModelCapacityError(
                f"完整请求保守估算 {estimated_input} token，上下文 {context_limit} 无生成余量；未发送请求。"
            )
        output = min(output, remaining)
    return output


def output_arguments(route: CapacityRoute, tokens: int) -> dict[str, int]:
    """只发送所选型号正式支持的一个输出额度参数。"""
    profile = model_capacity(route.base_url, route.model)
    return {profile.output_parameter if profile else "max_tokens": tokens}
