"""阶段3:切片与文案的单元测试 + 真实出片集成测试。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.clipping.clipper import (
    ClipOptions,
    _build_audio_filter,
    _build_video_filter,
    _group_srt,
)
from app.db.models import ClipStatus, RoomMode
from app.publishing.copywriter import _decide_status, _fallback_copy

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

from app.core.config import settings

_HAS_FFMPEG = shutil.which(settings.ffmpeg_path) is not None


# ----------------------------- 纯逻辑 ----------------------------- #
def test_build_audio_filter() -> None:
    """启用 loudnorm 时滤镜串包含 loudnorm;关闭去静音时不含 silenceremove。"""
    af = _build_audio_filter(ClipOptions(loudnorm=True, remove_silence=False))
    assert "loudnorm" in af
    assert "silenceremove" not in af
    af2 = _build_audio_filter(ClipOptions(loudnorm=False, remove_silence=True))
    assert "silenceremove" in af2
    assert "areverse" in af2


def test_build_video_filter_vertical() -> None:
    """竖屏选项生成缩放+补边滤镜。"""
    vf = _build_video_filter(ClipOptions(vertical=True), None)
    assert "scale=1080:1920" in vf
    assert "pad=1080:1920" in vf
    assert _build_video_filter(ClipOptions(vertical=False), None) == ""


def test_group_srt_format() -> None:
    """SRT 生成包含序号与时间轴箭头。"""
    words = [(0.0, 0.5, "你"), (0.5, 1.0, "好"), (1.0, 1.5, "世"), (1.5, 2.0, "界")]
    srt = _group_srt(words, max_chars=2)
    assert "1\n" in srt
    assert "-->" in srt
    assert "你好" in srt


def test_decide_status() -> None:
    """各审核模式的状态决策符合预期。"""
    assert _decide_status(RoomMode.AUTO, True, 0.9, 0.85) == ClipStatus.READY
    assert _decide_status(RoomMode.AUTO, False, 0.9, 0.85) == ClipStatus.REJECTED
    assert _decide_status(RoomMode.SEMI, True, 0.9, 0.85) == ClipStatus.READY
    assert _decide_status(RoomMode.SEMI, True, 0.5, 0.85) == ClipStatus.REVIEWING
    assert _decide_status(RoomMode.MANUAL, True, 0.99, 0.85) == ClipStatus.REVIEWING


def test_fallback_copy_uses_keywords() -> None:
    """规则文案在命中关键词时点题,并带通用标签。"""
    copy = _fallback_copy("这波操作绝了,五杀!", "测试")
    assert copy.title
    assert "直播切片" in copy.tags
    assert copy.worth_publishing is True


# --------------------------- 集成:真实出片 --------------------------- #
def _make_test_ts(path: Path, duration: int = 6) -> bool:
    """生成一个带音视频的 MPEG-TS 测试片段。

    :param path: 输出 .ts 路径。
    :param duration: 时长(秒)。
    :returns: 成功返回 ``True``。
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
        f"testsrc=duration={duration}:size=320x240:rate=15",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        "-f",
        "mpegts",
        str(path),
    ]
    return subprocess.run(cmd, capture_output=True).returncode == 0 and path.exists()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 FFmpeg")
def test_produce_clip_end_to_end(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """端到端:候选 -> 切片 MP4 + 文案(纯规则)+ 待上传清单。"""
    import json
    from datetime import UTC, datetime, timedelta

    from app.clipping.clipper import produce_clip
    from app.core.paths import ready_to_upload_dir
    from app.db.models import (
        FinalClip,
        HighlightCandidate,
        LiveRoom,
        RawSegment,
        RecordingSession,
        RoomMode,
        Transcript,
    )
    from app.db.session import get_session
    from app.publishing.copywriter import generate_copy

    ts_file = tmp_path / "seg.ts"
    assert _make_test_ts(ts_file), "生成测试 TS 失败"

    base = datetime.now(UTC)
    with get_session() as db:
        room = LiveRoom(input_url="x", room_id=1, authorized=True, mode=RoomMode.AUTO)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id)
        db.add(session)
        db.flush()
        seg = RawSegment(
            session_id=session.id,
            seq=0,
            file_path=str(ts_file),
            start_ts=base,
            end_ts=base + timedelta(seconds=6),
            duration_s=6.0,
        )
        db.add(seg)
        db.flush()
        db.add(
            Transcript(
                segment_id=seg.id,
                language="zh",
                text="这波操作绝了五杀",
                words_json=json.dumps([{"w": "绝了", "start": 1.0, "end": 1.5}]),
            )
        )
        # 候选取片段内 1s~4s,峰值 2s。
        cand = HighlightCandidate(
            session_id=session.id,
            peak_ts=base + timedelta(seconds=2),
            start_ts=base + timedelta(seconds=1),
            end_ts=base + timedelta(seconds=4),
            highlight_score=0.9,
        )
        db.add(cand)
        db.flush()
        cand_id = cand.id

    clip = produce_clip(cand_id)
    assert Path(clip.file_path).exists()
    assert clip.duration_s and clip.duration_s > 1.0
    assert clip.content_hash

    finished = generate_copy(clip.id)
    assert finished.title
    # AUTO 模式 + worth_publishing 默认 True -> READY,且导出清单。
    assert finished.status == ClipStatus.READY
    manifest = ready_to_upload_dir() / f"clip_{clip.id}.json"
    assert manifest.exists()

    with get_session() as db:
        stored = db.get(FinalClip, clip.id)
        assert stored.tags_json
