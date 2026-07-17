"""BiliLiveCut 高性能加速后端 (Python 参考版,.

当 C 扩展不可用时使用本模块作为纯 Python 参考实现。
接口与 ``_c_speedups`` 保持一致,性能优于原有业务代码。

包含:
    - ``fast_char_bigrams(text) -> list[str]``
    - ``fast_cosine_similarity(vec_a, vec_b) -> float``
    - ``fast_match_keywords(text, patterns_tuple) -> list[str]``
    - ``fast_meme_count(texts_list, memes_tuple) -> int``
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any


def _build_automaton(patterns: Sequence[str]) -> dict[str, Any]:
    """构建纯 Python Aho-Corasick 自动机 (dict trie).

    :param patterns: 模式字符串序列。
    :returns: 自动机 dict{trie, fail, outputs}。
    """
    trie: list[dict[int, int]] = [{}]  # node index → {byte → child}
    outputs: list[list[str]] = [[]]  # node index → pattern list

    for pat in patterns:
        node = 0
        for ch in pat:
            cb = ord(ch)
            nxt = trie[node].get(cb)
            if nxt is None:
                nxt = len(trie)
                trie.append({})
                outputs.append([])
                trie[node][cb] = nxt
            node = nxt
        outputs[node].append(pat)

    # BFS 构建失败链接
    from collections import deque

    fail = [-1] * len(trie)
    queue: deque[int] = deque()

    for _c, child in trie[0].items():
        fail[child] = 0
        queue.append(child)

    while queue:
        r = queue.popleft()
        for c, child in trie[r].items():
            queue.append(child)
            f = fail[r]
            while f != -1 and trie[f].get(c) is None:
                f = fail[f]
            fail[child] = trie[f].get(c, 0) if f != -1 else 0
            outputs[child].extend(outputs[fail[child]])

    # 修改 trie 使得缺失边指向 next(fail)
    for node_idx in range(len(trie)):
        for c in range(256):
            if c not in trie[node_idx]:
                f = fail[node_idx] if node_idx > 0 else 0
                if node_idx == 0:
                    trie[node_idx][c] = 0
                elif f != -1 and c in trie[f]:
                    trie[node_idx][c] = trie[f][c]
                else:
                    n2 = fail[node_idx]
                    while n2 > 0 and c not in trie[n2]:
                        n2 = fail[n2]
                    trie[node_idx][c] = trie[n2].get(c, 0)

    return {"trie": trie, "fail": fail, "outputs": outputs}


def fast_ahocorasick_build(patterns: Sequence[str]) -> Any:
    """构建 Aho-Corasick 自动机 (与 C 扩展 API 兼容,返回 dict).

    :param patterns: 模式字符串序列。
    :returns: 自动机对象。
    """
    return _build_automaton(patterns)


def fast_ahocorasick_search(automaton: dict, text: str) -> list[str]:
    """用自动机搜索文本,返回所有命中的模式。与 C 扩展行为一致。

    :param automaton: 自动机。
    :param text: 文本。
    :returns: 命中模式列表 (可能与文本等长，连续命中每个字符各计一次)。
    """
    trie = automaton["trie"]
    outputs = automaton["outputs"]
    fail = automaton["fail"]
    results: list[str] = []
    node = 0
    for ch in text:
        cb = ord(ch)
        # Follow failure links for characters not in trie (matching C extension behavior)
        while node != 0 and cb not in trie[node]:
            node = fail[node] if node > 0 else 0
        node = trie[node].get(cb, 0)
        for pat in outputs[node]:
            results.append(pat)
    return results


def fast_aho_has_match(automaton: dict, text: str) -> bool:
    """快速判断是否有模式匹配 (有则提前终止)。

    :param automaton: 自动机。
    :param text: 文本。
    :returns: 是否存在匹配。
    """
    trie = automaton["trie"]
    outputs = automaton["outputs"]
    fail = automaton["fail"]
    node = 0
    for ch in text:
        cb = ord(ch)
        while node != 0 and cb not in trie[node]:
            node = fail[node] if node > 0 else 0
        node = trie[node].get(cb, 0)
        if outputs[node]:
            return True
    return False


def fast_char_bigrams(text: str) -> list[str]:
    """字符级 bigram 提取 (跳过空白)。

    :param text: 输入文本。
    :returns: bigram 字符串列表。
    """
    if len(text) < 2:
        return [text] if text else []
    chars = [ch for ch in text if ch > " "]
    if len(chars) < 2:
        return [chars[0]] if chars else []
    return [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]


def fast_cosine_similarity(vec_a: dict, vec_b: dict) -> float:
    """快速余弦相似度 (直接迭代 key)。

    :param vec_a: {str: float}。
    :param vec_b: {str: float}。
    :returns: 0-1 相似度。
    """
    dot = 0.0
    na = 0.0
    for k, va in vec_a.items():
        na += va * va
        vb = vec_b.get(k)
        if vb is not None:
            dot += va * vb
    if na == 0:
        return 0.0
    nb = sum(v * v for v in vec_b.values())
    if nb == 0:
        return 0.0
    sim = dot / (math.sqrt(na) * math.sqrt(nb))
    return min(sim, 1.0)


def fast_match_keywords(text: str, patterns: tuple[str, ...]) -> list[str]:
    """一次性构建 + 扫描,返回命中的关键词列表。

    :param text: 文本。
    :param patterns: 关键词元组。
    :returns: 命中关键词列表。
    """
    if not patterns or not text:
        return []
    am = _build_automaton(patterns)
    return fast_ahocorasick_search(am, text)


def fast_meme_count(texts: list[str], memes: tuple[str, ...]) -> int:
    """统计弹幕列表中命中梗词的条数。

    :param texts: 弹幕文本列表。
    :param memes: 梗词元组。
    :returns: 命中条数。
    """
    if not memes or not texts:
        return 0
    am = _build_automaton(memes)
    count = 0
    for t in texts:
        if fast_aho_has_match(am, t):
            count += 1
    return count
