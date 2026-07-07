"""阶段2 端到端集成测试:真实音频 + 数据库 -> 高光候选。

不依赖 Whisper(直接写入转写记录)与 LLM(无 Key 时走纯规则),
但会用 FFmpeg 生成真实音频以覆盖音频特征链路;FFmpeg 不可用时跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.core.config import settings

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

_HAS_FFMPEG = shutil.which(settings.ffmpeg_path) is not None


def _make_burst_wav(path: Path) -> bool:
    """用 FFmpeg 生成"静音-爆响-静音"的 4 秒音频。

    :param path: 输出 wav 路径。
    :returns: 生成成功返回 ``True``。
    """
    cmd = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1:sample_rate=16000",
        "-af",
        "adelay=1500,apad=pad_dur=1.5",
        "-ac",
        "1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0 and path.exists()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 FFmpeg 才能生成测试音频")
def test_score_segment_creates_candidate(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """规则分达阈值时,score_segment 应生成一个高光候选并落库。"""
    from datetime import UTC, datetime

    from app.analysis.highlight import score_segment
    from app.db.models import (
        LiveRoom,
        RawSegment,
        RecordingSession,
        Transcript,
    )
    from app.db.session import get_session

    wav = tmp_path / "burst.wav"
    assert _make_burst_wav(wav), "FFmpeg 生成测试音频失败"

    # 降低阈值,确保规则分能稳定触发(不依赖具体权重数值)。
    monkeypatch.setattr(settings, "highlight_init_threshold", 0.0)

    now = datetime.now(UTC)
    with get_session() as db:
        room = LiveRoom(input_url="x", room_id=999, authorized=True, highlight_threshold=0.1)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id)
        db.add(session)
        db.flush()
        segment = RawSegment(
            session_id=session.id,
            seq=0,
            file_path=str(wav),
            start_ts=now,
            end_ts=now,
            duration_s=4.0,
        )
        db.add(segment)
        db.flush()
        # 富含关键词的转写 + 词级时间戳。
        words = [{"w": "卧槽", "start": 1.6, "end": 1.8}, {"w": "绝了", "start": 1.9, "end": 2.1}]
        db.add(
            Transcript(
                segment_id=segment.id,
                language="zh",
                text="卧槽这波操作绝了,直接五杀,笑死哈哈哈",
                words_json=json.dumps(words, ensure_ascii=False),
            )
        )
        db.flush()
        seg_id = segment.id

    candidate = score_segment(seg_id)
    assert candidate is not None
    assert candidate.highlight_score >= 0.1
    assert candidate.start_ts < candidate.end_ts
    # 未配置 LLM Key 时,llm_score 应为 0(纯规则)。
    assert candidate.llm_score == 0.0
