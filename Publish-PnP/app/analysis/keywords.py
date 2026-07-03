"""高光关键词匹配。

从 ``config/keywords.zh.txt`` 加载关键词表(每行一个,``#`` 为注释),
对转写文本做命中统计,产出 0-1 的关键词维度得分。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from loguru import logger

_KEYWORDS_PATH = Path(__file__).resolve().parents[2] / "config" / "keywords.zh.txt"

# 命中达到该数量即视为满分(避免长文本刷分)。
_HIT_CAP = 3


@lru_cache(maxsize=1)
def load_keywords() -> tuple[str, ...]:
    """加载关键词表(缓存)。

    :returns: 关键词元组;文件缺失时返回空元组。
    """
    if not _KEYWORDS_PATH.exists():
        logger.warning("未找到关键词表 {}。", _KEYWORDS_PATH)
        return ()
    words: list[str] = []
    for line in _KEYWORDS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line)
    return tuple(words)


def match_keywords(text: str) -> tuple[float, list[str]]:
    """统计文本中的关键词命中并给出得分。

    :param text: 转写文本。
    :returns: ``(score, hits)``,``score`` 为 0-1,``hits`` 为命中的关键词列表。
    """
    if not text:
        return 0.0, []
    lowered = text.lower()
    hits = [kw for kw in load_keywords() if kw.lower() in lowered]
    score = min(len(hits) / _HIT_CAP, 1.0)
    return score, hits
