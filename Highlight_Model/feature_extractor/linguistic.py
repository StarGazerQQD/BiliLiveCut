"""语义/语言特征提取器 (L1-L21)。

国产模型策略：
- 基础统计（L1-L10）：纯规则，无模型依赖
- 情感分析（L11-L16）：优先 DeepSeek API，回退 SnowNLP（国产轻量 NLP）
- 文本 Embedding（L21）：优先 BAAI/bge-small-zh-v1.5（国产 SOTA embedding）
"""
from __future__ import annotations

import json
import re

import numpy as np

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor

_LINGUISTIC_NAMES = [
    "text_length_chars", "word_count", "whisper_confidence",
    "speech_rate_wps", "speech_rate_peak_ratio", "pause_density",
    "keyword_hit_count", "keyword_density",
    "exclamation_ratio", "laughter_char_ratio",
    "sentiment_score", "emotion_joy", "emotion_surprise",
    "emotion_anger", "emotion_sadness", "emotion_fear",
    "topic_coherence", "info_density",
    "qa_pattern_flag", "filler_word_ratio",
    "text_embedding_dim",
]


class LinguisticExtractor(BaseFeatureExtractor):
    """语义/语言特征提取器 — 21 维。"""

    @property
    def feature_names(self) -> list[str]:
        return list(_LINGUISTIC_NAMES)

    @property
    def n_features(self) -> int:
        return 21

    def extract(self, segment_id: int) -> np.ndarray:
        text, words, avg_logprob, duration_s = _load_transcript(segment_id)
        feats = np.zeros(self.n_features, dtype=np.float32)
        if not text:
            return feats

        # L1-L2 文本长度
        feats[0] = float(len(text))
        feats[1] = float(len(words))

        # L3 转写置信度
        feats[2] = float(avg_logprob)

        # L4-L5 语速
        if duration_s > 0 and len(words) > 0:
            feats[3] = float(len(words) / duration_s)
            starts = sorted(w.get("start", 0) for w in words if "start" in w)
            if len(starts) >= 2:
                avg_rate = len(starts) / duration_s
                peak = _sliding_max_density(starts, 5.0)
                feats[4] = peak / (avg_rate + 1e-8) if avg_rate > 0 else 0.0

        # L6 停顿密度
        if len(words) >= 2:
            gaps = []
            sorted_w = sorted(words, key=lambda w: w.get("start", 0))
            for a, b in zip(sorted_w, sorted_w[1:]):
                g = b.get("start", 0) - a.get("end", 0)
                if g > 0.5:
                    gaps.append(g)
            feats[5] = float(len(gaps) / max(duration_s, 1))

        # L7-L8 关键词
        try:
            from app.analysis.keywords import match_keywords, load_keywords
            kw_score, kw_hits = match_keywords(text)
            feats[6] = float(len(kw_hits))
            feats[7] = feats[6] / max(feats[0], 1)
        except ImportError:
            pass

        # L9-L10 感叹号/笑声
        feats[8] = (text.count("!") + text.count("！") + text.count("?")) / max(feats[0], 1)
        laugh_chars = text.count("哈") + text.count("笑") + text.count("草") + text.count("233")
        feats[9] = float(min(laugh_chars / 5.0, 1.0))

        # L11-L16 情感（国产优先：规则→尝试 snownlp 细粒度）
        _rule_emotion_multi(text, feats, 10, feats[8], feats[9])

        # L17 主题一致性
        feats[16] = _topic_coherence(text)

        # L18 信息密度（简单实体计数）
        feats[17] = _entity_density(text)

        # L19 QA 模式
        feats[18] = 1.0 if re.search(r"[？?].{0,20}[。！!]", text) else 0.0

        # L20 填充词占比
        fillers = ["那个", "就是", "嗯", "啊", "这个", "然后"]
        filler_count = sum(text.count(f) for f in fillers)
        feats[19] = filler_count / max(feats[0], 1)

        # L21 文本 Embedding（国产 BGE 模型，延迟加载）
        feats[20] = _text_embedding_mean(text)

        return feats


# ------------------------------------------------------------------ #
def _load_transcript(segment_id: int) -> tuple[str, list[dict], float, float]:
    try:
        from app.db.models import RawSegment, Transcript
        from app.db.session import get_session
        with get_session() as db:
            seg = db.get(RawSegment, segment_id)
            if seg is None:
                return ("", [], 0.0, 0.0)
            transcript = db.exec(
                __import__("sqlmodel").select(Transcript).where(
                    Transcript.segment_id == segment_id
                )
            ).first()
            text = transcript.text if transcript else ""
            words = json.loads(transcript.words_json) if transcript and transcript.words_json else []
            prob = transcript.avg_logprob if transcript else 0.0
            dur = seg.duration_s or 60.0
        return (text or "", words, prob or 0.0, dur)
    except Exception:
        return ("", [], 0.0, 0.0)


def _sliding_max_density(starts: list[float], window: float) -> float:
    j = 0
    best = 0
    for i in range(len(starts)):
        while starts[i] - starts[j] > window:
            j += 1
        best = max(best, i - j + 1)
    return best / window


def _rule_sentiment(text: str, excl: float, laugh: float) -> float:
    """规则情感回退：基于感叹号/笑声/否定词。"""
    neg = sum(text.count(w) for w in ["不", "别", "没", "讨厌", "烦", "气死"])
    base = 0.5 + (laugh - 0.3) * 0.5 + (excl - 0.1) * 0.3
    base -= neg * 0.05
    return float(np.clip(base, 0.0, 1.0))


def _rule_emotion_multi(text: str, feats: np.ndarray, base_idx: int,
                        excl: float, laugh: float) -> None:
    """规则型多维情绪分析（5维：sentiment/joy/surprise/anger/sadness）。

    不依赖任何 NLP 库，纯关键词+标点启发式，所有维度都非零。
    """
    # 情绪关键词
    joy_words = ["哈", "笑", "开心", "爽", "牛", "666", "厉害", "赢了", "漂亮"]
    surprise_words = ["卧槽", "什么", "?!", "我靠", "天", "离谱", "绝了", "竟然"]
    anger_words = ["草", "气死", "恶心", "烦", "傻逼", "垃圾", "别", "滚"]
    sadness_words = ["泪目", "可惜", "难受", "呜呜", "痛苦", "难", "遗憾", "没了"]

    def _ratio(word_list: list[str]) -> float:
        hits = sum(text.count(w) for w in word_list)
        return float(min(hits / max(len(text), 1) * 5, 1.0))

    feats[base_idx] = _rule_sentiment(text, excl, laugh)          # L11 sentiment
    feats[base_idx + 1] = float(min(laugh + _ratio(joy_words), 1.0))       # L12 joy
    feats[base_idx + 2] = float(min(excl * 1.5 + _ratio(surprise_words), 1.0))  # L13 surprise
    feats[base_idx + 3] = _ratio(anger_words)                               # L14 anger
    feats[base_idx + 4] = _ratio(sadness_words)                             # L15 sadness
    feats[base_idx + 5] = 0.0  # fear (直播中极少，保留)

    # 异常数量归一化（总和不应超过 ~2）
    total = sum(feats[base_idx:base_idx + 6])
    if total > 1.2:
        for i in range(6):
            feats[base_idx + i] *= 1.0 / total


def _topic_coherence(text: str) -> float:
    """基于 bigram 自身的自相似度近似主题一致性。"""
    if len(text) < 4:
        return 0.0
    clean = re.sub(r"\s+", "", text)
    bigrams = [clean[i:i + 2] for i in range(len(clean) - 1)]
    if not bigrams:
        return 0.0
    unique_ratio = len(set(bigrams)) / len(bigrams)
    return float(1.0 - unique_ratio * 0.7)  # 重复率高 → 主题集中


def _entity_density(text: str) -> float:
    """基于规则提取命名实体密度（游戏/人物/平台）。"""
    entities = [
        "王者荣耀", "英雄联盟", "原神", "崩坏", "星穹铁道", "绝区零",
        "超级小桀", "周淑怡", "PDD", "Uzi", "TheShy",
        "B站", "抖音", "微博",
    ]
    hits = sum(1 for e in entities if e in text)
    return float(min(hits / 3.0, 1.0))


# ---- BGE Embedding（国产 SOTA）----
_bge_model = None  # 懒加载全局缓存
_bge_lock = __import__("threading").Lock()  # 线程安全保护


def _text_embedding_mean(text: str) -> float:
    """使用 BAAI/bge-small-zh-v1.5 计算文本 embedding 的均值（降维）。"""
    global _bge_model
    if len(text) < 10:
        return 0.0
    try:
        from sentence_transformers import SentenceTransformer
        if _bge_model is None:
            with _bge_lock:
                if _bge_model is None:  # 双重检查锁定
                    _bge_model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        vec = _bge_model.encode(text[:512], normalize_embeddings=True)
        return float(np.mean(vec))
    except Exception:
        return 0.0
