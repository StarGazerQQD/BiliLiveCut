"""多大模型配置测试:优先级排序、可用筛选、key 保留合并、失败回退。"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
        id=name, name=name, base_url="https://x/v1", api_key=key,
        model="m", priority=priority, enabled=enabled,
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
    provs.save_providers([
        _p("ok", 1),
        _p("off", 2, enabled=False),
        _p("nokey", 3, key=""),
    ])
    assert [p.name for p in provs.active_providers()] == ["ok"]


def test_merge_and_save_preserves_key(temp_db: None) -> None:
    """未提供新 key(空或掩码)的条目应沿用旧 key;提供则更新。

    :param temp_db: 隔离数据库夹具。
    """
    provs.save_providers([_p("A", 1, key="secret-key")])
    pid = provs.load_providers()[0].id

    # 提交时 key 留空 -> 保留旧 key;另改名。
    provs.merge_and_save([
        {"id": pid, "name": "A2", "base_url": "https://x/v1", "model": "m",
         "api_key": "", "priority": 1, "enabled": True},
    ])
    p = provs.load_providers()[0]
    assert p.name == "A2"
    assert p.api_key == "secret-key"

    # 掩码占位也视为不修改。
    provs.merge_and_save([
        {"id": pid, "name": "A2", "base_url": "https://x/v1", "model": "m",
         "api_key": "****key", "priority": 1, "enabled": True},
    ])
    assert provs.load_providers()[0].api_key == "secret-key"

    # 提供新 key -> 更新。
    provs.merge_and_save([
        {"id": pid, "name": "A2", "base_url": "https://x/v1", "model": "m",
         "api_key": "brand-new", "priority": 1, "enabled": True},
    ])
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
