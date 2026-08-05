"""多大模型配置测试:优先级排序、可用筛选、key 保留合并、失败回退。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from app.analysis import llm as llm_mod
from app.analysis import llm_providers as provs

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _p(name: str, priority: int, key: str = "k", enabled: bool = True) -> provs.LLMProvider:
    """构造一个测试用 provider。

    :param name: 名称。
    :param priority: 优先级。
    :param key: api_key。
    :param enabled: 是否启用。
    :returns: provider。
    """
    return provs.LLMProvider(
        id=name,
        name=name,
        base_url="https://x/v1",
        api_key=key,
        model="m",
        priority=priority,
        enabled=enabled,
    )


def test_save_load_and_priority_sort(temp_db: None) -> None:
    """保存后应按优先级升序加载。

    :param temp_db: 隔离数据库夹具。
    """
    provs.save_providers([_p("B", 10), _p("A", 1), _p("C", 5)])
    loaded = provs.load_providers()
    assert [p.name for p in loaded] == ["A", "C", "B"]


def test_active_filters_disabled_and_keyless(temp_db: None) -> None:
    """可用列表应排除未启用与缺 key 的条目。

    :param temp_db: 隔离数据库夹具。
    """
    provs.save_providers(
        [
            _p("ok", 1),
            _p("off", 2, enabled=False),
            _p("nokey", 3, key=""),
        ]
    )
    assert [p.name for p in provs.active_providers()] == ["ok"]


def test_merge_and_save_preserves_key(temp_db: None) -> None:
    """未提供新 key(空或掩码)的条目应沿用旧 key;提供则更新。

    :param temp_db: 隔离数据库夹具。
    """
    provs.save_providers([_p("A", 1, key="secret-key")])
    pid = provs.load_providers()[0].id

    # 提交时 key 留空 -> 保留旧 key;另改名。
    provs.merge_and_save(
        [
            {
                "id": pid,
                "name": "A2",
                "base_url": "https://x/v1",
                "model": "m",
                "api_key": "",
                "priority": 1,
                "enabled": True,
            },
        ]
    )
    p = provs.load_providers()[0]
    assert p.name == "A2"
    assert p.api_key == "secret-key"

    # 掩码占位也视为不修改。
    provs.merge_and_save(
        [
            {
                "id": pid,
                "name": "A2",
                "base_url": "https://x/v1",
                "model": "m",
                "api_key": "****key",
                "priority": 1,
                "enabled": True,
            },
        ]
    )
    assert provs.load_providers()[0].api_key == "secret-key"

    # 提供新 key -> 更新。
    provs.merge_and_save(
        [
            {
                "id": pid,
                "name": "A2",
                "base_url": "https://x/v1",
                "model": "m",
                "api_key": "brand-new",
                "priority": 1,
                "enabled": True,
            },
        ]
    )
    assert provs.load_providers()[0].api_key == "brand-new"


def test_public_view_masks_key(temp_db: None) -> None:
    """对外视图不含明文 key,仅含 set 标志。

    :param temp_db: 隔离数据库夹具。
    """
    provs.save_providers([_p("A", 1, key="abcdef1234")])
    view = provs.public_view()[0]
    assert "api_key" not in view
    assert view["api_key_set"] is True


def test_load_falls_back_to_env(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """未配置多模型时,回退到 .env 单模型配置。

    :param temp_db: 隔离数据库夹具。
    :param monkeypatch: pytest 夹具。
    """
    monkeypatch.setattr(provs.settings, "llm_api_key", "env-key", raising=False)
    monkeypatch.setattr(provs.settings, "llm_model", "deepseek-chat", raising=False)
    monkeypatch.setattr(provs.settings, "anthropic_api_key", "", raising=False)
    loaded = provs.load_providers()
    assert len(loaded) == 1
    assert loaded[0].id == "env"
    assert loaded[0].api_key == "env-key"


def test_call_text_failover(monkeypatch: MonkeyPatch) -> None:
    """首个 provider 失败时应自动降级到下一个并返回其结果。

    :param monkeypatch: pytest 夹具。
    """
    p1, p2 = _p("P1", 1), _p("P2", 2)
    monkeypatch.setattr(provs, "active_providers", lambda: [p1, p2])
    monkeypatch.setattr(llm_mod.provs, "active_providers", lambda: [p1, p2])

    def fake_complete(provider, prompt, max_tokens, extra_body=None):
        if provider.name == "P1":
            raise RuntimeError("boom")
        return "from-P2"

    monkeypatch.setattr(llm_mod, "_complete", fake_complete)
    assert llm_mod.call_text("hi") == "from-P2"


def test_call_text_all_fail_returns_none(monkeypatch: MonkeyPatch) -> None:
    """所有 provider 都失败时返回 None。

    :param monkeypatch: pytest 夹具。
    """
    p1 = _p("P1", 1)
    monkeypatch.setattr(llm_mod.provs, "active_providers", lambda: [p1])

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(llm_mod, "_complete", boom)
    assert llm_mod.call_text("hi") is None


def test_extract_text_supports_openai_content_parts() -> None:
    """兼容服务返回文本分块时应拼接最终正文，忽略非文本块。"""
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=[
                        {"type": "text", "text": "第一段"},
                        SimpleNamespace(type="text", text="第二段"),
                        {"type": "image_url", "image_url": "https://example.invalid/image"},
                    ]
                )
            )
        ]
    )

    assert llm_mod._extract_text(response) == "第一段\n第二段"


def test_complete_retries_reasoning_only_deepseek_without_thinking(monkeypatch: MonkeyPatch) -> None:
    """DeepSeek 仅返回推理且最终正文为空时，应关闭思考模式重试。"""
    provider = provs.LLMProvider(
        id="deepseek",
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        model="deepseek-v4-pro",
    )
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content=None, reasoning_content="内部推理，不应返回"),
                )
            ],
            usage=SimpleNamespace(
                completion_tokens=128,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=128),
            ),
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"clean_text":"正文","summary":"摘要"}'),
                )
            ],
            usage=None,
        ),
    ]
    calls: list[dict[str, object]] = []

    def create(**kwargs: object) -> object:
        calls.append(kwargs)
        return responses.pop(0)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm_mod, "_get_client", lambda _provider: client)

    result = llm_mod._complete(provider, "整理转写", max_tokens=4096)

    assert result == '{"clean_text":"正文","summary":"摘要"}'
    assert len(calls) == 2
    assert "extra_body" not in calls[0]
    assert calls[1]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_complete_rejects_repeated_empty_content(monkeypatch: MonkeyPatch) -> None:
    """服务连续返回空正文时应抛出可诊断错误，不能被当作连通成功。"""
    provider = _p("empty", 1)
    empty_response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="  "))],
        usage=None,
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: empty_response)))
    monkeypatch.setattr(llm_mod, "_get_client", lambda _provider: client)

    with pytest.raises(llm_mod.EmptyLLMResponseError, match="连续两次返回空正文"):
        llm_mod._complete(provider, "ping", max_tokens=64)


def test_extract_json_array_salvages_complete_items_from_truncated_output() -> None:
    """数组尾部被截断时应保留此前已经完整闭合的对象。"""
    raw = '[{"title":"第一条"},{"title":"第二条"},{"title":"未完成"'

    assert llm_mod.extract_json_array(raw) == [{"title": "第一条"}, {"title": "第二条"}]


def test_judge_highlight_recovers_score_from_truncated_reason(monkeypatch: MonkeyPatch) -> None:
    """理由被截断时，已完整生成的高光布尔值和评分不应被丢弃。"""
    monkeypatch.setattr(
        llm_mod,
        "call_text",
        lambda *_args, **_kwargs: '{"is_highlight": true, "score": 1.4, "reason": "高音量配合',
    )

    result = llm_mod.judge_highlight("测试", {"volume": 0.9})

    assert result is not None
    assert result.is_highlight is True
    assert result.score == 1.0
    assert result.reason == "高音量配合"


def test_judge_highlight_uses_configured_token_budget(monkeypatch: MonkeyPatch) -> None:
    """五分钟转写的高光复核应使用可配置的万级输出预算。"""
    calls: list[int] = []

    def fake_call_text(_prompt: str, max_tokens: int = 512) -> str:
        calls.append(max_tokens)
        return '{"is_highlight":false,"score":0.1,"reason":"普通片段"}'

    monkeypatch.setattr(llm_mod, "call_text", fake_call_text)
    monkeypatch.setattr(llm_mod.settings, "highlight_llm_max_tokens", 65536)

    result = llm_mod.judge_highlight("五分钟转写", {"volume": 0.1})

    assert result is not None
    assert calls == [65536]


def test_judge_highlight_restores_offsets_to_original_segment(monkeypatch: MonkeyPatch) -> None:
    """局部转写窗口返回的偏移必须换算回原始录制分段。"""
    prompts: list[str] = []

    def fake_call_text(prompt: str, max_tokens: int = 512) -> str:
        prompts.append(prompt)
        return '{"is_highlight":true,"score":0.9,"reason":"窗口内爆点","start_offset":2,"end_offset":8}'

    monkeypatch.setattr(llm_mod, "call_text", fake_call_text)

    result = llm_mod.judge_highlight("只包含候选窗口的正文", {"volume": 0.9}, "", 120.0)

    assert result is not None
    assert result.suggested_start_offset == pytest.approx(122.0)
    assert result.suggested_end_offset == pytest.approx(128.0)
    assert "120.000 秒" in prompts[0]


def test_judge_highlight_preserves_negative_cross_segment_window_offset(monkeypatch: MonkeyPatch) -> None:
    """跨前一分段的窗口起点可为负数，换算时不得错误钳制到当前分段起点。"""
    from app.analysis import llm as llm_mod

    monkeypatch.setattr(
        llm_mod,
        "call_text",
        lambda *_args, **_kwargs: (
            '{"is_highlight":true,"score":0.9,"reason":"跨段爆点","start_offset":2,"end_offset":18}'
        ),
    )

    result = llm_mod.judge_highlight("跨分段上下文", {"danmaku": 0.9}, "", -30.0)

    assert result is not None
    assert result.suggested_start_offset == pytest.approx(-28.0)
    assert result.suggested_end_offset == pytest.approx(-12.0)


def test_judge_highlight_discards_non_finite_offsets(monkeypatch: MonkeyPatch) -> None:
    """模型异常返回 NaN/Infinity 时不得污染候选时间边界。"""
    monkeypatch.setattr(
        llm_mod,
        "call_text",
        lambda *_args, **_kwargs: (
            '{"is_highlight":true,"score":0.9,"reason":"窗口内爆点","start_offset":NaN,"end_offset":Infinity}'
        ),
    )

    result = llm_mod.judge_highlight("候选正文", {"volume": 0.9}, "", 120.0)

    assert result is not None
    assert result.suggested_start_offset is None
    assert result.suggested_end_offset is None


def test_refine_transcript_requires_complete_clean_text_and_summary(monkeypatch: MonkeyPatch) -> None:
    """转写整理仅接受同时包含可读正文与摘要的 JSON。"""
    monkeypatch.setattr(
        llm_mod,
        "call_text",
        lambda *_args, **_kwargs: '{"clean_text":"整理后的正文。","summary":"本段摘要"}',
    )

    result = llm_mod.refine_transcript("没有标点的原始转写")

    assert result == llm_mod.TranscriptRefinement(clean_text="整理后的正文。", summary="本段摘要")

    monkeypatch.setattr(llm_mod, "call_text", lambda *_args, **_kwargs: '{"clean_text":"只有正文"}')
    assert llm_mod.refine_transcript("原始转写") is None


def test_refine_transcript_prompt_requests_conservative_repetition_cleanup(monkeypatch: MonkeyPatch) -> None:
    """整理提示词应清除 ASR 边界复读，同时明确保留真实口语强调。"""
    captured: dict[str, object] = {}

    def fake_call_text(prompt: str, max_tokens: int = 512) -> str:
        captured["prompt"] = prompt
        captured["max_tokens"] = max_tokens
        return '{"clean_text":"整理后的正文。","summary":"本段摘要"}'

    monkeypatch.setattr(llm_mod, "call_text", fake_call_text)

    result = llm_mod.refine_transcript("等一下我们先看看等一下我们先看看")

    assert result is not None
    assert "ASR 解码或 VAD 分句边界产生的连续重复词句" in str(captured["prompt"])
    assert "保留主播有明确语义的强调、复述和口头禅" in str(captured["prompt"])
    assert captured["max_tokens"] == 65536
