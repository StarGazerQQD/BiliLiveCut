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

        # L11-L16 情感（国产优先：snownlp → 规则回退）
        try:
            from snownlp import SnowNLP
            s = SnowNLP(text)
            feats[10] = float(s.sentiments)  # sentiment_score 0-1
            # SnowNLP 只能二分，不区分情绪维度
            joy_hint = feats[9] + feats[8]  # 笑声+感叹号 → joy
            feats[11] = float(min(joy_hint, 1.0))  # emotion_joy
            feats[12] = 0.0  # surprise
            feats[13] = 0.0  # anger
            feats[14] = 0.0  # sadness
            feats[15] = 0.0  # fear
        except ImportError:
            # 规则回退：基于笑声/感叹号/关键词粗糙估计
            feats[10] = _rule_sentiment(text, feats[8], feats[9])

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


def _text_embedding_mean(text: str) -> float:
    """使用 BAAI/bge-small-zh-v1.5 计算文本 embedding 的均值（降维）。"""
    global _bge_model
    if len(text) < 10:
        return 0.0
    try:
        from sentence_transformers import SentenceTransformer
        if _bge_model is None:
            _bge_model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        vec = _bge_model.encode(text[:512], normalize_embeddings=True)
        return float(np.mean(vec))
    except Exception:
        return 0.0
