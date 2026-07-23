"""P2 房间级配置工具。

每个直播间可配置:
- hotwords: Whisper 热词/纠错列表。
- aliases: 专有名词替换映射(如 {"thp":"审判"} )。
- highlight_keywords: 规则评分额外关键词。
- blocked_topics: 不适合生成切片的屏蔽话题模式。
- recording_paused: 人工暂停自动录制,恢复时创建新会话。

配置存储在 ``LiveRoom.room_config_json`` 中。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from copy import deepcopy

from app.db.models import LiveRoom

_DEFAULT_CONFIG: dict = {
    "hotwords": [],
    "aliases": {},
    "highlight_keywords": [],
    "blocked_topics": [],
    "highlight_ml_mode": "inherit",
    "recording_paused": False,
}

_HIGHLIGHT_ML_MODES = frozenset({"inherit", "off", "shadow", "champion"})


def load_room_config(room: LiveRoom | None) -> dict:
    """从房间加载配置,不存在或解析失败时返回默认空配置。

    :param room: ``LiveRoom`` 实例或 None。
    :returns: 配置字典。
    """
    if room is None or not room.room_config_json:
        return deepcopy(_DEFAULT_CONFIG)
    try:
        parsed = json.loads(room.room_config_json)
        # 确保所有预期键存在。
        cfg = deepcopy(_DEFAULT_CONFIG)
        cfg.update(parsed)
        return cfg
    except (json.JSONDecodeError, TypeError):
        return deepcopy(_DEFAULT_CONFIG)


def merge_room_config(room: LiveRoom, updates: dict[str, object]) -> dict[str, object]:
    """合并并校验房间配置，避免局部更新清除未知设置。"""
    merged: dict[str, object] = load_room_config(room)
    merged.update(updates)
    mode = merged.get("highlight_ml_mode", "inherit")
    if not isinstance(mode, str) or mode not in _HIGHLIGHT_ML_MODES:
        raise ValueError("highlight_ml_mode 必须是 inherit/off/shadow/champion")
    merged["highlight_ml_mode"] = mode
    paused = merged.get("recording_paused", False)
    if not isinstance(paused, bool):
        raise ValueError("recording_paused 必须是布尔值")
    merged["recording_paused"] = paused
    return merged


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
