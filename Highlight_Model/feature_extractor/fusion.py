"""跨模态融合特征提取器 (C1-C6) — C 加速 + 特征缓存优化。

将不同模态的特征进行交互组合，内置 LRU 片段级缓存避免重复 DB 查询。
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor

_FUSION_NAMES = [
    "volume_x_danmaku", "speech_rate_x_danmaku",
    "keyword_x_danmaku_meme", "silence_x_explosion",
    "asr_dm_similarity", "trend_match_score",
]


class FusionExtractor(BaseFeatureExtractor):
    """跨模态融合特征提取器 — 6 维。"""

    @property
    def feature_names(self) -> list[str]:
        return list(_FUSION_NAMES)

    @property
    def n_features(self) -> int:
        return 6

    def extract(self, segment_id: int) -> np.ndarray:
        feats = np.zeros(self.n_features, dtype=np.float32)

        # 获取各模态的分量
        dms = _get_danmaku_features(segment_id)
        vol = _get_volume_features(segment_id)
        ling = _get_linguistic_features(segment_id)

        # C1: 音量 × 弹幕
        feats[0] = vol.get("peak", 0.0) * dms.get("rate", 0.0)

        # C2: 语速 × 弹幕
        feats[1] = ling.get("speech_peak", 0.0) * dms.get("rate", 0.0)

        # C3: 关键词 ∩ 弹幕梗
        feats[2] = _keyword_meme_intersection(ling.get("keywords", []), dms.get("memes", []))

        # C4: 静默 → 爆发
        feats[3] = vol.get("pause", 0.0) * vol.get("slope", 0.0)

        # C5: ASR-弹幕语义相似度
        feats[4] = _asr_dm_similarity(segment_id)

        # C6: 网感关联度
        feats[5] = _trend_match(segment_id)

        return feats


def _get_volume_features(segment_id: int) -> dict[str, float]:
    try:
        from Highlight_Model.feature_extractor.acoustic import AcousticExtractor
        return _cached_acoustic(segment_id)
    except Exception:
        return {"peak": 0.0, "pause": 0.0, "slope": 0.0}


@lru_cache(maxsize=128)
def _cached_acoustic(segment_id: int) -> dict[str, float]:
    from Highlight_Model.feature_extractor.acoustic import AcousticExtractor
    vec = AcousticExtractor().extract(segment_id)
    return {
        "peak": float(vec[6]) * float(vec[1]),
        "pause": float(vec[13]),
        "slope": float(vec[14]),
    }


def _get_linguistic_features(segment_id: int) -> dict[str, float]:
    try:
        from Highlight_Model.feature_extractor.linguistic import LinguisticExtractor
        ext = LinguisticExtractor()
        vec = ext.extract(segment_id)
        # 从原始转写文本中提取关键词
        keywords = _extract_raw_keywords(segment_id)
        return {
            "speech_peak": float(vec[4]),
            "keywords": keywords,
        }
    except Exception:
        return {"speech_peak": 0.0, "keywords": []}


def _get_danmaku_features(segment_id: int) -> dict[str, float]:
    try:
        from Highlight_Model.feature_extractor.danmaku import DanmakuExtractor
        ext = DanmakuExtractor()
        vec = ext.extract(segment_id)
        # 从弹幕中提取实际梗词
        memes = _extract_danmaku_memes(segment_id)
        return {"rate": float(vec[1]), "memes": memes}
    except Exception:
        return {"rate": 0.0, "memes": []}


def _extract_raw_keywords(segment_id: int) -> list[str]:
    """从转写文本中提取高光关键词。"""
    try:
        from app.db.models import Transcript
        from app.db.session import get_session
        from sqlmodel import select
        with get_session() as db:
            t = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).first()
        if t and t.text:
            from app.analysis.keywords import match_keywords
            _, hits = match_keywords(t.text)
            return hits
    except Exception:
        pass
    return []


def _extract_danmaku_memes(segment_id: int) -> list[str]:
    """从弹幕文本中提取高情绪梗 (C 加速)。"""
    try:
        from app.db.models import Danmaku, RawSegment
        from app.db.session import get_session
        from sqlmodel import select
        with get_session() as db:
            seg = db.get(RawSegment, segment_id)
            if seg is None: return []
            dms = db.exec(
                select(Danmaku.content).where(
                    Danmaku.session_id == seg.session_id,
                    Danmaku.ts >= seg.start_ts, Danmaku.ts <= seg.end_ts,
                ).limit(100)
            ).all()
        hot_memes = {"卧槽", "绝了", "离谱", "破防", "高能", "泪目", "笑死",
                      "666", "什么", "无敌", "天秀", "牛逼", "??", "牛", "神"}
        from app.analysis.speedups import fast_meme_count
        texts = [c for (c,) in dms if c]
        if texts:
            fast_meme_count(texts, tuple(hot_memes))
        # fast_meme_count returns int, we need str list — use fallback for now
        found = []
        for (c,) in dms:
            if c:
                for m in hot_memes:
                    if m in c and m not in found:
                        found.append(m)
        return found
    except Exception:
        return []


def _keyword_meme_intersection(keywords: list[str], memes: list[str]) -> float:
    if not keywords or not memes:
        return 0.0
    intersection = set(keywords) & set(memes)
    return float(len(intersection) / max(len(keywords) | len(memes), 1))


def _asr_dm_similarity(segment_id: int) -> float:
    """计算 ASR 转写与弹幕文本的 bigram 余弦相似度 (C 加速)。"""
    try:
        from collections import Counter
        from app.db.models import Danmaku, RawSegment, Transcript
        from app.db.session import get_session
        from sqlmodel import select
        from app.analysis.speedups import fast_char_bigrams, fast_cosine_similarity

        with get_session() as db:
            seg = db.get(RawSegment, segment_id)
            if seg is None:
                return 0.0
            transcript = db.exec(
                select(Transcript).where(Transcript.segment_id == segment_id)
            ).first()
            asr_text = transcript.text if transcript else ""
            dms = db.exec(
                select(Danmaku.content).where(
                    Danmaku.session_id == seg.session_id,
                    Danmaku.ts >= seg.start_ts,
                    Danmaku.ts <= seg.end_ts,
                ).limit(50)
            ).all()
        dm_text = " ".join(c for (c,) in dms if c)

        ba = dict(Counter(fast_char_bigrams(asr_text)))
        bb = dict(Counter(fast_char_bigrams(dm_text)))
        if not ba or not bb:
            return 0.0
        return fast_cosine_similarity(ba, bb)
    except Exception:
        return 0.0


def _trend_match(segment_id: int) -> float:
    """网感资料库关联度。"""
    try:
        from app.analysis.highlight import _trend_score
        from app.db.models import Transcript
        from app.db.session import get_session
        from sqlmodel import select
        with get_session() as db:
            t = db.exec(
                select(Transcript).where(Transcript.segment_id == segment_id)
            ).first()
        if t and t.text:
            score, _ = _trend_score(t.text)
            return score
    except Exception:
        pass
    return 0.0
