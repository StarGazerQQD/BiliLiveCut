"""声学特征提取器 (A1-A38)。

基于片段的 PCM 音频数据，使用 librosa 提取频谱、MFCC、基频等。
若无 librosa，回退到纯 numpy 实现（覆盖前 15 维）。纯本地计算，无 API 依赖。
"""
from __future__ import annotations

import math

import numpy as np

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor

_ACOUSTIC_NAMES = [
    "rms_mean", "rms_median", "rms_std",
    "rms_p25", "rms_p75", "rms_p90",
    "crest_factor", "energy_entropy", "short_term_energy_ratio", "rms_delta_max",
    "silence_ratio", "silence_count", "avg_silence_duration",
    "pause_before_peak", "peak_slope",
    "spectral_centroid", "spectral_bandwidth", "spectral_rolloff",
    *(f"mfcc_{i}" for i in range(1, 14)),
    "zero_crossing_rate",
    "pitch_mean", "pitch_std", "pitch_range",
    "harmonic_noise_ratio",
    "jitter", "shimmer",
]


class AcousticExtractor(BaseFeatureExtractor):
    """声学特征提取器 — 38 维。"""

    @property
    def feature_names(self) -> list[str]:
        return list(_ACOUSTIC_NAMES)

    @property
    def n_features(self) -> int:
        return 38

    def extract(self, segment_id: int) -> np.ndarray:
        pcm = _load_pcm(segment_id)
        feats = np.zeros(self.n_features, dtype=np.float32)
        if pcm.size == 0:
            return feats
        sr = 16000
        duration_s = pcm.size / sr

        times, rms_norm = _rms_envelope(pcm, sr)
        if rms_norm.size > 0:
            _fill_rms_features(feats, rms_norm)
            _fill_silence_features(feats, times, rms_norm)
            _fill_peak_slope(feats, rms_norm)

        try:
            import librosa
            _librosa_extract(feats, pcm, sr, duration_s)
        except ImportError:
            _pitch_fallback(feats, pcm, sr, duration_s)

        return feats


# ------------------------------------------------------------------ #
def _load_pcm(segment_id: int) -> np.ndarray:
    try:
        from app.analysis.audio import extract_pcm
        from app.db.models import RawSegment
        from app.db.session import get_session
        with get_session() as db:
            seg = db.get(RawSegment, segment_id)
            if seg is None or not seg.file_path:
                return np.zeros(0, dtype=np.float32)
            path = seg.file_path
        return extract_pcm(path)
    except Exception:
        return np.zeros(0, dtype=np.float32)


def _rms_envelope(pcm: np.ndarray, sr: int, hop_s: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    if pcm.size == 0:
        return np.zeros(0), np.zeros(0)
    hop = max(1, int(sr * hop_s))
    n_frames = pcm.size // hop
    if n_frames == 0:
        frames = pcm[np.newaxis, :]
        n_frames = 1
    else:
        frames = pcm[:n_frames * hop].reshape(n_frames, hop)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1) + 1e-12).astype(np.float32)
    peak = float(np.max(rms))
    if peak > 1e-9:
        rms = rms / peak
    times = (np.arange(n_frames, dtype=np.float32) + 0.5) * hop_s
    return times, rms


def _fill_rms_features(feats: np.ndarray, rms_norm: np.ndarray) -> None:
    feats[0] = float(np.mean(rms_norm))
    feats[1] = float(np.median(rms_norm))
    feats[2] = float(np.std(rms_norm))
    feats[3] = float(np.percentile(rms_norm, 25))
    feats[4] = float(np.percentile(rms_norm, 75))
    feats[5] = float(np.percentile(rms_norm, 90))
    peak, mean = float(np.max(rms_norm)), float(np.mean(rms_norm))
    feats[6] = peak / (mean + 1e-8)
    if rms_norm.sum() > 1e-9:
        p = rms_norm / (rms_norm.sum() + 1e-12)
        p = p[p > 0]
        feats[7] = float(-np.sum(p * np.log(p + 1e-12)))
    n_short = min(10, rms_norm.size)
    feats[8] = float(np.max(rms_norm[:n_short])) / (mean + 1e-8)
    if rms_norm.size >= 2:
        feats[9] = float(np.max(np.diff(rms_norm)))


def _fill_silence_features(feats: np.ndarray, times: np.ndarray, rms_norm: np.ndarray) -> None:
    silences = _detect_silences(times, rms_norm)
    total = max(rms_norm.size, 1)
    quiet = np.sum(rms_norm < 0.15) if rms_norm.size > 0 else 0
    feats[10] = quiet / total
    feats[11] = float(len(silences))
    durs = [e - s for s, e in silences]
    feats[12] = np.mean(durs) if durs else 0.0
    if rms_norm.size > 0 and times.size > 0:
        peak_i = int(np.argmax(rms_norm))
        pt = times[peak_i]
        pre = [pt - e for s, e in silences if 0 < pt - e < 5.0]
        feats[13] = pre[0] if pre else 0.0


def _detect_silences(times: np.ndarray, rms: np.ndarray,
                     thresh: float = 0.15, min_dur: float = 0.3) -> list[tuple[float, float]]:
    if rms.size == 0:
        return []
    hop = float(times[1] - times[0]) if times.size > 1 else 0.1
    quiet = rms < thresh
    result: list[tuple[float, float]] = []
    start: int | None = None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * hop >= min_dur:
                result.append((float(times[start]), float(times[i - 1])))
            start = None
    if start is not None and (len(quiet) - start) * hop >= min_dur:
        result.append((float(times[start]), float(times[-1])))
    return result


def _fill_peak_slope(feats: np.ndarray, rms_norm: np.ndarray) -> None:
    if rms_norm.size < 2:
        return
    peak_i = int(np.argmax(rms_norm))
    if peak_i > 0:
        feats[14] = float(rms_norm[peak_i] - rms_norm[peak_i - 1])


def _librosa_extract(feats: np.ndarray, pcm: np.ndarray, sr: int,
                     duration_s: float) -> None:
    import librosa
    pcm64 = pcm.astype(np.float64)
    # 频谱
    S = np.abs(librosa.stft(pcm64, n_fft=1024, hop_length=160))
    feats[15] = float(librosa.feature.spectral_centroid(S=S, sr=sr).mean())
    feats[16] = float(librosa.feature.spectral_bandwidth(S=S, sr=sr).mean())
    feats[17] = float(librosa.feature.spectral_rolloff(S=S, sr=sr).mean())
    # MFCC
    mfcc = librosa.feature.mfcc(y=pcm64, sr=sr, n_mfcc=13, n_fft=1024, hop_length=160)
    for i in range(13):
        feats[18 + i] = float(np.mean(mfcc[i]))
    # ZCR
    feats[31] = float(librosa.feature.zero_crossing_rate(pcm64).mean())
    # Pitch
    f0, voiced, _ = librosa.pyin(pcm64, fmin=75, fmax=500, sr=sr,
                                  frame_length=1024, hop_length=160)
    f0_v = f0[voiced] if voiced is not None and voiced.any() else np.zeros(0)
    if f0_v.size > 0:
        feats[32] = float(np.mean(f0_v))
        feats[33] = float(np.std(f0_v))
        feats[34] = float(np.max(f0_v) - np.min(f0_v))
    else:
        _pitch_fallback(feats, pcm, sr, duration_s)
    # HNR
    try:
        h = librosa.effects.harmonic(pcm64, margin=3.0)
        p = librosa.effects.percussive(pcm64, margin=3.0)
        feats[35] = float(10 * math.log10((np.sum(h**2) / (np.sum(p**2) + 1e-8)) + 1e-8))
    except Exception:
        feats[35] = 0.0
    # Jitter/Shimmer
    if f0_v.size >= 2:
        d = np.abs(np.diff(f0_v))
        feats[36] = float(np.mean(d) / (np.mean(f0_v) + 1e-8))
    feats[37] = 0.0


def _pitch_fallback(feats: np.ndarray, pcm: np.ndarray, sr: int,
                    duration_s: float) -> None:
    pitches: list[float] = []
    win, hop = int(sr * 0.04), int(sr * 0.02)
    for start in range(0, min(pcm.size - win, int(duration_s * sr)), hop):
        f = pcm[start:start + win].astype(np.float64)
        f -= np.mean(f)
        c = np.correlate(f, f, mode="full")
        c = c[len(c) // 2:]
        if c[0] < 1e-9:
            continue
        c /= c[0]
        lo, hi = max(1, int(sr / 500)), min(len(c) - 1, int(sr / 75))
        if lo >= hi:
            continue
        pi = int(np.argmax(c[lo:hi])) + lo
        if c[pi] > 0.3:
            pitches.append(sr / pi)
    if pitches:
        a = np.array(pitches, dtype=np.float32)
        feats[32] = float(np.mean(a))
        feats[33] = float(np.std(a))
        feats[34] = float(np.max(a) - np.min(a))
