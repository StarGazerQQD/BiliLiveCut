"""阶段3:切片与文案的单元测试 + 真实出片集成测试。"""

from __future__ import annotations

import json
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
    _run_ffmpeg_clip,
    _write_concat_list,
)
from app.db.models import ClipStatus, RawSegment
from app.publishing.copywriter import _decide_status, _fallback_copy, gather_clip_text

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

from app.core.config import settings

_HAS_FFMPEG = shutil.which(settings.ffmpeg_path) is not None


# ----------------------------- 纯逻辑 ----------------------------- #
def test_build_audio_filter() -> None:
    """音频后处理必须在最后归零时间轴。"""
    af = _build_audio_filter(ClipOptions(loudnorm=True, remove_silence=False))
    assert "loudnorm" in af
    assert "silenceremove" not in af
    assert af.endswith("asetpts=PTS-STARTPTS")
    af2 = _build_audio_filter(ClipOptions(loudnorm=False, remove_silence=True))
    assert "silenceremove" in af2
    assert "areverse" in af2
    assert af2.endswith("asetpts=PTS-STARTPTS")
    assert _build_audio_filter(ClipOptions(loudnorm=False, remove_silence=False)) == "asetpts=PTS-STARTPTS"


def test_build_video_filter_vertical() -> None:
    """视频滤镜始终归零首帧，竖屏选项另生成缩放与补边。"""
    vf = _build_video_filter(ClipOptions(vertical=True), None)
    assert "scale=1080:1920" in vf
    assert "pad=1080:1920" in vf
    assert vf.startswith("setpts=PTS-STARTPTS")
    assert _build_video_filter(ClipOptions(vertical=False), None) == "setpts=PTS-STARTPTS"


def test_group_srt_format() -> None:
    """SRT 生成包含序号与时间轴箭头。"""
    words = [(0.0, 0.5, "你"), (0.5, 1.0, "好"), (1.0, 1.5, "世"), (1.5, 2.0, "界")]
    srt = _group_srt(words, max_chars=2)
    assert "1\n" in srt
    assert "-->" in srt
    assert "你好" in srt


def test_group_srt_respects_line_gap() -> None:
    """兼容实现按字幕模板的停顿阈值断句。"""
    words = [(0.0, 0.2, "你"), (0.5, 0.7, "好")]
    srt = _group_srt(words, max_chars=20, line_gap_ms=200)
    assert "1\n" in srt
    assert "2\n" in srt
    assert "你好" not in srt


def test_decide_status() -> None:
    """auto_* 开关的状态决策符合预期。"""
    # auto_approve=on, worth, score >= threshold → READY
    assert _decide_status(True, 0.82, 0.9, True) == ClipStatus.READY
    # auto_approve=on, not worth → REVIEWING
    assert _decide_status(True, 0.82, 0.9, False) == ClipStatus.REVIEWING
    # auto_approve=on, score < threshold → REVIEWING
    assert _decide_status(True, 0.82, 0.5, True) == ClipStatus.REVIEWING
    # auto_approve=off → REVIEWING (不管分数和 worth)
    assert _decide_status(False, 0.82, 0.99, True) == ClipStatus.REVIEWING


def test_fallback_copy_uses_keywords() -> None:
    """规则文案在命中关键词时点题,并带通用标签。"""
    copy = _fallback_copy("这波操作绝了,五杀!", "测试")
    assert copy.title
    assert "直播切片" in copy.tags
    assert copy.worth_publishing is True


def test_gather_clip_text_excludes_content_after_final_window(temp_db: None) -> None:
    """文案只能读取最终成片时间窗，不能吸入同一五分钟分段的后续内容。"""
    import json
    from datetime import UTC, datetime, timedelta

    from app.db.models import HighlightCandidate, HighlightEvent, RawSegment, RecordingSession, Transcript
    from app.db.session import get_session

    base = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    with get_session() as db:
        session = RecordingSession(room_id=1, started_at=base)
        db.add(session)
        db.flush()
        segment = RawSegment(
            session_id=session.id,
            seq=7,
            file_path="five-minute.ts",
            start_ts=base,
            end_ts=base + timedelta(seconds=300),
            duration_s=300,
        )
        db.add(segment)
        db.flush()
        db.add(
            Transcript(
                segment_id=segment.id,
                text="片头闲聊。真正的成片正文。成片结束后才发生的下一件事。",
                words_json=json.dumps(
                    [
                        {"w": "片头闲聊", "start": 20, "end": 25},
                        {"w": "真正的成片正文", "start": 82, "end": 90},
                        {"w": "成片结束后才发生的下一件事", "start": 210, "end": 220},
                    ],
                    ensure_ascii=False,
                ),
            )
        )
        candidate = HighlightCandidate(
            session_id=session.id,
            peak_ts=base + timedelta(seconds=90),
            start_ts=base + timedelta(seconds=60),
            end_ts=base + timedelta(seconds=150),
            highlight_score=0.9,
            reason="候选评分理由",
            dedup_hash="copy-window-regression",
        )
        db.add(candidate)
        db.flush()
        db.add(
            HighlightEvent(
                candidate_id=candidate.id,
                session_id=session.id,
                segment_id=segment.id,
                raw_start_ts=candidate.start_ts,
                raw_end_ts=candidate.end_ts,
                adjusted_start_ts=base + timedelta(seconds=75),
                adjusted_end_ts=base + timedelta(seconds=105),
            )
        )
        db.flush()
        candidate_id = candidate.id

    assert candidate_id is not None
    text, reason = gather_clip_text(candidate_id)

    assert text == "真正的成片正文"
    assert "片头闲聊" not in text
    assert "成片结束后" not in text
    assert reason == "候选评分理由"


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


def _video_start_times(path: Path) -> tuple[float, float]:
    """返回视频流起点和首个实际解码帧的 PTS。"""
    stream_cmd = [
        settings.ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=start_time",
        "-of",
        "json",
        str(path),
    ]
    stream_data = json.loads(subprocess.run(stream_cmd, capture_output=True, check=True, text=True).stdout)
    frame_cmd = [
        settings.ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-read_intervals",
        "%+#1",
        "-show_entries",
        "frame=pts_time",
        "-of",
        "json",
        str(path),
    ]
    frame_data = json.loads(subprocess.run(frame_cmd, capture_output=True, check=True, text=True).stdout)
    return float(stream_data["streams"][0]["start_time"]), float(frame_data["frames"][0]["pts_time"])


def _stream_packet_count(path: Path, stream: str) -> int:
    """返回指定流的 packet 数量，用于验证无损导出没有丢包。"""
    command = [
        settings.ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        stream,
        "-count_packets",
        "-show_entries",
        "stream=nb_read_packets",
        "-of",
        "json",
        str(path),
    ]
    data = json.loads(subprocess.run(command, capture_output=True, check=True, text=True).stdout)
    return int(data["streams"][0]["nb_read_packets"])


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 FFmpeg")
def test_ts_to_mp4_starts_on_real_video_frame(tmp_path: Path) -> None:
    """TS 的 AAC 预滚不得让 MP4 在首个真实画面之前产生黑帧空窗。"""
    ts_file = tmp_path / "source.ts"
    assert _make_test_ts(ts_file, duration=3), "生成测试 TS 失败"
    segment = RawSegment(session_id=1, seq=0, file_path=str(ts_file))
    concat_list = _write_concat_list([segment], tmp_path)
    output = tmp_path / "normalized.mp4"

    _run_ffmpeg_clip(
        concat_list,
        output,
        0.0,
        2.0,
        ClipOptions(
            loudnorm=False,
            remove_silence=False,
            vertical=False,
            subtitle=False,
            preset="veryfast",
        ),
        None,
    )

    stream_start, first_frame_pts = _video_start_times(output)
    assert stream_start == pytest.approx(0.0, abs=0.001)
    assert first_frame_pts == pytest.approx(0.0, abs=0.001)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 FFmpeg")
def test_lossless_source_export_rebases_video_without_dropping_packets(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """源 TS 无损导出应校准首个画面，同时完整保留音视频包。"""
    from app.core import paths as path_module
    from app.db.models import Transcript
    from app.db.session import get_session
    from app.web.services import transcripts as transcript_service

    storage_root = tmp_path / "storage"
    source_dir = storage_root / "raw" / "session_1"
    source_dir.mkdir(parents=True)
    source = source_dir / "source.ts"
    assert _make_test_ts(source, duration=3), "生成测试 TS 失败"
    monkeypatch.setattr(path_module.settings, "storage_root", str(storage_root))

    with get_session() as db:
        segment = RawSegment(session_id=1, seq=0, file_path=str(source))
        db.add(segment)
        db.flush()
        transcript = Transcript(segment_id=segment.id, text="无损导出")
        db.add(transcript)
        db.flush()
        transcript_id = transcript.id

    source_video_packets = _stream_packet_count(source, "v:0")
    source_audio_packets = _stream_packet_count(source, "a:0")
    output = transcript_service.remux_transcript_source(transcript_id)
    stream_start, first_frame_pts = _video_start_times(output)

    assert output.parent == (storage_root / "clips" / "source_exports").resolve()
    assert stream_start == pytest.approx(0.0, abs=0.001)
    assert first_frame_pts == pytest.approx(0.0, abs=0.001)
    assert _stream_packet_count(output, "v:0") == source_video_packets
    assert _stream_packet_count(output, "a:0") == source_audio_packets
    assert transcript_service.remux_transcript_source(transcript_id) == output


def test_source_export_cleanup_does_not_fail_when_old_download_is_open(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Windows 正在下载旧缓存时，清理失败不得推翻已成功的新导出。"""
    from app.web.services import transcripts as transcript_service

    source = tmp_path / "source.ts"
    source.write_bytes(b"ts")
    export_root = tmp_path / "exports"
    export_root.mkdir()
    output = export_root / "segment_7_new.mp4"
    stale = export_root / "segment_7_old.mp4"
    stale.write_bytes(b"old")

    monkeypatch.setattr(transcript_service, "_probe_stream_start_times", lambda _source: (1.5, 1.45))

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"new")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    original_unlink = Path.unlink

    def guarded_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == stale:
            raise PermissionError("download in progress")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(transcript_service.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "unlink", guarded_unlink)

    transcript_service._render_source_export(source, output, 7, export_root)

    assert output.read_bytes() == b"new"
    assert stale.read_bytes() == b"old"


def test_source_export_reports_missing_ffmpeg(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """FFmpeg 不可执行时应返回领域错误，并清理未完成文件。"""
    from app.web.services import transcripts as transcript_service

    source = tmp_path / "source.ts"
    source.write_bytes(b"ts")
    export_root = tmp_path / "exports"
    export_root.mkdir()
    output = export_root / "segment_8_new.mp4"
    monkeypatch.setattr(transcript_service, "_probe_stream_start_times", lambda _source: (1.5, 1.45))
    monkeypatch.setattr(
        transcript_service.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("ffmpeg")),
    )

    with pytest.raises(transcript_service.TranscriptMediaError, match="无法启动 FFmpeg"):
        transcript_service._render_source_export(source, output, 8, export_root)

    assert not list(export_root.glob("*.partial.mp4"))


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
        Transcript,
    )
    from app.db.session import get_session
    from app.publishing.copywriter import generate_copy

    ts_file = tmp_path / "seg.ts"
    assert _make_test_ts(ts_file), "生成测试 TS 失败"

    base = datetime.now(UTC)
    with get_session() as db:
        room = LiveRoom(input_url="x", room_id=1, authorized=True, auto_approve=True)
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
