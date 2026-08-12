"""P2 房间级配置工具。

每个直播间可配置:
- hotwords: Whisper 热词/纠错列表。
- aliases: 专有名词替换映射(如 {"thp":"审判"} )。
- highlight_keywords: 规则评分额外关键词。
- blocked_topics: 不适合生成切片的屏蔽话题模式。
- recording_paused: 人工暂停自动录制,恢复时创建新会话。
- recording_auto_restart_suppressed: 人工停止后阻止监控器立即重新拉起。
- recording_wait_for_next_live: 重连预算耗尽后等待一次真实离线再允许自动录制。
- highlight_scorer_mode: 高光评分插件的房间级模式覆盖。

配置存储在 ``LiveRoom.room_config_json`` 中。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from copy import deepcopy

from app.db.entities import LiveRoom

_DEFAULT_CONFIG: dict = {
    "hotwords": [],
    "aliases": {},
    "learned_aliases": {},
    "highlight_keywords": [],
    "blocked_topics": [],
    "recording_paused": False,
    "recording_auto_restart_suppressed": False,
    "recording_wait_for_next_live": False,
    "highlight_scorer_mode": "inherit",
}
_CONFIG_KEYS = frozenset(_DEFAULT_CONFIG)


def load_room_config(room: LiveRoom | None) -> dict[str, object]:
    """从房间加载当前格式的配置。

    :param room: ``LiveRoom`` 实例或 None。
    :returns: 配置字典。
    :raises ValueError: 已保存的 JSON 不是当前格式。
    """
    if room is None or not room.room_config_json:
        return deepcopy(_DEFAULT_CONFIG)
    try:
        parsed = json.loads(room.room_config_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("room_config_json 不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("room_config_json 必须是对象")
    missing = _CONFIG_KEYS - set(parsed)
    unknown = set(parsed) - _CONFIG_KEYS
    if missing:
        raise ValueError(f"room_config_json 缺少当前格式字段: {sorted(missing)}")
    if unknown:
        raise ValueError(f"room_config_json 包含未知字段: {sorted(unknown)}")
    return _validate_room_config(parsed)


def merge_room_config(room: LiveRoom, updates: dict[str, object]) -> dict[str, object]:
    """合并并校验当前房间配置。"""
    unknown = set(updates) - _CONFIG_KEYS
    if unknown:
        raise ValueError(f"room_config 包含未知字段: {sorted(unknown)}")
    merged: dict[str, object] = load_room_config(room)
    merged.update(updates)
    return _validate_room_config(merged)


def _validate_room_config(merged: dict[str, object]) -> dict[str, object]:
    """严格校验当前房间配置的所有字段。"""
    missing = _CONFIG_KEYS - set(merged)
    unknown = set(merged) - _CONFIG_KEYS
    if missing:
        raise ValueError(f"room_config 缺少当前格式字段: {sorted(missing)}")
    if unknown:
        raise ValueError(f"room_config 包含未知字段: {sorted(unknown)}")

    paused = merged["recording_paused"]
    if not isinstance(paused, bool):
        raise ValueError("recording_paused 必须是布尔值")
    merged["recording_paused"] = paused
    for key in ("recording_auto_restart_suppressed", "recording_wait_for_next_live"):
        value = merged[key]
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        merged[key] = value
    scoring_mode = merged["highlight_scorer_mode"]
    if not isinstance(scoring_mode, str):
        raise ValueError("highlight_scorer_mode 必须是字符串")
    if scoring_mode not in {"inherit", "off", "shadow", "champion"}:
        raise ValueError("highlight_scorer_mode 必须是 inherit/off/shadow/champion")
    merged["highlight_scorer_mode"] = scoring_mode
    merged["hotwords"] = _string_list(merged["hotwords"], name="hotwords", limit=500)
    merged["highlight_keywords"] = _string_list(merged["highlight_keywords"], name="highlight_keywords", limit=500)
    merged["blocked_topics"] = _string_list(merged["blocked_topics"], name="blocked_topics", limit=500)
    merged["aliases"] = _alias_map(merged["aliases"], name="aliases")
    merged["learned_aliases"] = _alias_map(merged["learned_aliases"], name="learned_aliases")
    return merged


def effective_hotwords(config: dict[str, object]) -> list[str]:
    """返回人工热词和纠错目标词合并后的稳定去重列表。"""
    aliases = _alias_map(config["aliases"], name="aliases")
    learned = _alias_map(config["learned_aliases"], name="learned_aliases")
    values: list[str] = list(_string_list(config["hotwords"], name="hotwords", limit=500))
    for mapping in (aliases, learned):
        values.extend(mapping.values())
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def learn_room_aliases(room: LiveRoom, aliases: dict[str, str]) -> dict[str, object]:
    """把人工转写纠错沉淀到当前直播间词典。"""
    validated = _alias_map(aliases, name="aliases")
    config = load_room_config(room)
    existing = _alias_map(config["aliases"], name="aliases")
    learned = _alias_map(config["learned_aliases"], name="learned_aliases")
    existing.update(validated)
    learned.update(validated)
    hotwords = _string_list(config["hotwords"], name="hotwords", limit=500)
    hotwords = list(dict.fromkeys([*hotwords, *validated.values()]))
    return merge_room_config(
        room,
        {
            "aliases": existing,
            "learned_aliases": learned,
            "hotwords": hotwords,
        },
    )


def _string_list(value: object, *, name: str, limit: int) -> list[str]:
    """校验房间配置中的短字符串列表。"""
    if not isinstance(value, list):
        raise ValueError(f"{name} 必须是字符串列表")
    if len(value) > limit:
        raise ValueError(f"{name} 最多允许 {limit} 项")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{name} 只能包含字符串")
        normalized = item.strip()
        if not normalized:
            continue
        if len(normalized) > 80:
            raise ValueError(f"{name} 单项最多 80 个字符")
        result.append(normalized)
    return list(dict.fromkeys(result))


def _alias_map(value: object, *, name: str) -> dict[str, str]:
    """校验房间配置中的纠错映射。"""
    if not isinstance(value, dict) or len(value) > 500:
        raise ValueError(f"{name} 必须是至多 500 项的对象")
    result: dict[str, str] = {}
    for wrong, correct in value.items():
        if not isinstance(wrong, str) or not isinstance(correct, str):
            raise ValueError(f"{name} 的键和值必须是字符串")
        source = wrong.strip()
        target = correct.strip()
        if not source or not target:
            continue
        if len(source) > 80 or len(target) > 80:
            raise ValueError(f"{name} 单项最多 80 个字符")
        if source != target:
            result[source] = target
    return result


def apply_aliases(text: str, aliases: dict[str, str]) -> str:
    """对文本应用别名替换。

    :param text: 原始文本。
    :param aliases: ``{错误写法: 正确写法}`` 映射。
    :returns: 替换后的文本。
    """
    if not aliases:
        return text
    # 按键长度降序替换,优先匹配长词。
    for wrong, correct in sorted(aliases.items(), key=lambda x: -len(x[0])):
        text = re.sub(re.escape(wrong), correct, text, flags=re.IGNORECASE)
    return text


def match_extra_keywords(text: str, extra_keywords: Sequence[str]) -> list[str]:
    """在文本中匹配额外的高光关键词。

    :param text: 文本。
    :param extra_keywords: 额外关键词列表。
    :returns: 命中关键词列表。
    """
    hits = []
    if not extra_keywords:
        return hits
    for kw in extra_keywords:
        if kw and kw in text:
            hits.append(kw)
    return hits


def is_blocked_topic(text: str, blocked_patterns: Sequence[str]) -> bool:
    """检查文本是否命中屏蔽话题。

    :param text: 文本。
    :param blocked_patterns: 屏蔽模式列表。
    :returns: 命中任一模式时返回 True。
    """
    if not blocked_patterns:
        return False
    for pattern in blocked_patterns:
        if pattern and pattern in text:
            return True
    return False
