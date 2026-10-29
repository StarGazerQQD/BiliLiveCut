from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.analysis.llm_providers import LLMProvider
from app.models.capacity import (
    DEFAULT_OUTPUT_TOKENS,
    MODEL_CAPACITIES,
    ModelCapacityError,
    capacity_identity,
    model_capacity,
    output_arguments,
    output_budget,
)
from app.models.contracts import ModelCapacity, ModelRoute


def route(base_url: str = "https://custom.invalid/v1", model: str = "custom") -> LLMProvider:
    return LLMProvider(id="capacity", name="容量", base_url=base_url, model=model, api_key="unused")


@pytest.mark.parametrize("profile", MODEL_CAPACITIES, ids=lambda item: item.id)
def test_catalog_capacity_respects_explicit_lower_limit_and_exact_endpoint(profile: ModelCapacity) -> None:
    provider = route(profile.base_urls[0], profile.models[0])
    assert model_capacity(provider.base_url + "/", provider.model) == profile
    assert (
        model_capacity(provider.base_url.replace("https://", "HTTPS://").replace(".com", ".COM"), provider.model)
        == profile
    )
    assert model_capacity(provider.base_url + "/proxy", provider.model) is None
    assert model_capacity("https://proxy.invalid/v1", provider.model) is None
    provider.max_output_tokens = 256
    assert output_budget(provider, DEFAULT_OUTPUT_TOKENS, [{"role": "user", "content": "正文"}]) == 256
    assert output_arguments(provider, 256) == {profile.output_parameter: 256}


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.deepseek.com",
        "https://api.deepseek.com.invalid",
        "https://api.deepseek.com:444",
        "https://user:secret@api.deepseek.com",
        "https://api.deepseek.com?proxy=1",
        "https://api.deepseek.com/#other",
    ],
)
def test_nearby_or_credential_endpoints_do_not_inherit_official_capacity(endpoint: str) -> None:
    assert model_capacity(endpoint, "deepseek-flash") is None


def test_default_https_port_and_case_preserve_official_model_cap() -> None:
    provider = route("https://DASHSCOPE.ALIYUNCS.COM:443/compatible-mode/v1", "qwen-plus")
    assert output_budget(provider, DEFAULT_OUTPUT_TOKENS, []) == 32768


@pytest.mark.parametrize(
    ("endpoint", "model", "expected"),
    [
        ("https://api.deepseek.com", "deepseek-flash", 393216),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus", 32768),
        ("https://api.minimax.cn/v1", "MiniMax-M3.1-Flash", 524288),
        ("https://api.xiaomimimo.com/v1", "mimo-v2.6-pro", 131072),
    ],
)
def test_official_output_caps_override_excessive_manual_values(endpoint: str, model: str, expected: int) -> None:
    provider = route(endpoint, model)
    provider.max_output_tokens = 2097152
    assert output_budget(provider, DEFAULT_OUTPUT_TOKENS, [{"role": "user", "content": "简短正文"}]) == expected


def test_shared_context_accounts_for_multibyte_messages_and_full_response_schema() -> None:
    provider = route("https://api.moonshot.cn/v1", "kimi-k3")
    messages = [{"role": "system", "content": "完整系统说明"}, {"role": "user", "content": "中文素材" * 5000}]
    schema = {"type": "json_schema", "json_schema": {"schema": {"enum": ["引用" * 2000]}}}
    estimate = len(json.dumps([messages, schema], ensure_ascii=False, separators=(",", ":")).encode()) + 512
    assert output_budget(provider, 1048576, messages, schema) == 1048576 - estimate
    assert output_budget(provider, 1048576, messages, schema) < output_budget(provider, 1048576, messages)


def test_custom_capacity_preserves_old_defaults_and_allows_large_explicit_limits() -> None:
    provider = route()
    assert output_budget(provider, DEFAULT_OUTPUT_TOKENS, []) == 65536
    before = capacity_identity(provider)
    provider.max_output_tokens = 800000
    assert output_budget(provider, DEFAULT_OUTPUT_TOKENS, []) == 800000
    provider.context_window_tokens = 1048576
    provider.max_input_tokens = 800000
    assert capacity_identity(provider) != before
    assert output_budget(provider, DEFAULT_OUTPUT_TOKENS, [{"role": "user", "content": "x" * 300000}]) < 750000


def test_input_and_context_limits_fail_before_a_request_and_do_not_truncate_source() -> None:
    provider = route()
    messages = [{"role": "user", "content": "长中文素材" * 200}]
    provider.max_input_tokens = 128
    with pytest.raises(ModelCapacityError, match="超过输入限制.*未发送请求"):
        output_budget(provider, 65536, messages)
    provider.max_input_tokens = None
    provider.context_window_tokens = 1024
    with pytest.raises(ModelCapacityError, match="无生成余量.*未发送请求"):
        output_budget(provider, 65536, messages)
    assert messages[0]["content"] == "长中文素材" * 200


def test_route_old_json_and_extended_limits_preserve_explicit_values() -> None:
    original = {"id": "route", "name": "原路由", "capability": "analysis", "connection_id": "shared", "model": "custom"}
    old = ModelRoute.model_validate(original | {"max_output_tokens": 4096})
    assert old.context_window_tokens is None and old.max_input_tokens is None and old.max_output_tokens == 4096
    large = ModelRoute.model_validate(
        original | {"context_window_tokens": 2097152, "max_input_tokens": 1000000, "max_output_tokens": 1048576}
    )
    assert ModelRoute.model_validate_json(large.model_dump_json()) == large
    for field in ("context_window_tokens", "max_input_tokens", "max_output_tokens"):
        with pytest.raises(ValidationError):
            ModelRoute.model_validate(original | {field: 2097153})
        with pytest.raises(ValidationError, match="ASR 路由"):
            ModelRoute.model_validate(original | {"capability": "asr", field: 2048})


def test_capacity_provenance_round_trips_old_records_and_rejects_secret_fields() -> None:
    from app.analysis.editorial_provenance import ReviewProvenance, capture_provenance

    provider = route("https://api.deepseek.com", "deepseek-flash")
    policy = {
        "policy": "test",
        "prompt": "a" * 64,
        "routes": [
            {
                "id": "r",
                "connection": "c",
                "model": provider.model,
                "tokens": None,
                "priority": 1,
                "capacity": capacity_identity(provider),
            }
        ],
        "attempts": 1,
        "deadline": 1800.0,
        "request_timeout": 600.0,
        "max_tokens": 1048576,
    }
    captured = capture_provenance(policy, None, operation_id="b" * 32, prior_attempts=[])
    assert captured.policy["routes"][0]["capacity"] == capacity_identity(provider)
    original = captured.model_dump(mode="json")
    original["policy"]["routes"][0].pop("capacity")
    assert ReviewProvenance.model_validate(original).policy == original["policy"]
    original["policy"]["routes"][0]["capacity"] = capacity_identity(provider) | {"api_key": "private"}
    with pytest.raises(ValidationError):
        ReviewProvenance.model_validate(original)
