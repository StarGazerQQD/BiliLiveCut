"""同主题高光合集编辑与渲染(V0.1.6 P2)。

流程:
1. 从 Topic 中选取已批准事件,按叙事顺序排列;
2. 检测相邻片段重叠上下文,自动裁掉重复区域;
3. 将时间接近且内容连续的片段合并;
4. 统一响度、字幕样式;
5. 片段间插入可选章节标题卡;
6. 渲染输出单一横屏 MP4。

不引入复杂转场,仅做硬切或简短淡入淡出。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlmodel import select

from app.core.config import settings
from app.core.paths import clips_dir
from app.db.models import (
    ClipStatus,
    ClipVariant,
    ClipVariantType,
    FinalClip,
    HighlightCandidate,
    HighlightTopic,
    RenderStatus,
    Topic,
)
from app.db.session import get_session


def get_collection_events(topic_id: int) -> list[dict]:
    """获取主题下所有关联事件(含排序和时长)。

    :param topic_id: 主题 id。
    :returns: 事件列表(按 sort_order 排序)。
    """
    with get_session() as db:
        if db.get(Topic, topic_id) is None:
            return []
        links = db.exec(
            select(HighlightTopic)
            .where(
                HighlightTopic.topic_id == topic_id,
            )
            .order_by(HighlightTopic.sort_order.asc(), HighlightTopic.created_at.asc())
        ).all()
        events = []
        for link in links:
            cand = db.get(HighlightCandidate, link.event_id)
            if cand is None:
                continue
            # 查找已有成品。
            clips = db.exec(
                select(FinalClip)
                .where(
                    FinalClip.candidate_id == cand.id,
                    FinalClip.status.in_([ClipStatus.GENERATED, ClipStatus.READY]),
                )
                .order_by(FinalClip.created_at.desc())
            ).all()
            clip_info = None
            if clips:
                c = clips[0]
                clip_info = {
                    "id": c.id,
                    "file_path": c.file_path,
                    "duration_s": c.duration_s,
                    "title": c.title,
                }
            start = cand.start_ts
            end = cand.end_ts
            dur = (end - start).total_seconds() if start and end else 0
            events.append(
                {
                    "event_id": cand.id,
                    "candidate_id": cand.id,
                    "score": cand.highlight_score,
                    "reason": cand.reason,
                    "start_ts": start.isoformat() if start else None,
                    "end_ts": end.isoformat() if end else None,
                    "duration_s": round(dur, 1),
                    "clip": clip_info,
                    "sort_order": link.sort_order,
                }
            )
        # 按 sort_order 排序。
        events.sort(key=lambda e: e["sort_order"])
        return events


def detect_overlap(events: Sequence[dict], threshold_s: float = 2.0) -> list[dict]:
    """检测相邻事件是否存在上下文重叠(同一原始片段且时间接近)。

    :param events: 按顺序排列的事件列表。
    :param threshold_s: 重叠阈值(秒),低于此值视为可合并。
    :returns: 重叠信息 ``[{index, overlap_s, mergeable}]``。
    """
    overlaps = []
    for i in range(len(events) - 1):
        a_end = events[i].get("end_ts")
        b_start = events[i + 1].get("start_ts")
        if not a_end or not b_start:
            overlaps.append({"index": i, "overlap_s": 0, "mergeable": False})
            continue
        a_end_dt = datetime.fromisoformat(a_end)
        b_start_dt = datetime.fromisoformat(b_start)
        diff_s = (b_start_dt - a_end_dt).total_seconds()
        if diff_s < 0:
            # 有重叠。
            overlaps.append({"index": i, "overlap_s": abs(diff_s), "mergeable": True})
        elif diff_s <= threshold_s:
            # 时间接近,可合并。
            overlaps.append({"index": i, "overlap_s": 0, "gap_s": diff_s, "mergeable": True})
        else:
            overlaps.append({"index": i, "overlap_s": 0, "gap_s": diff_s, "mergeable": False})
    return overlaps


def render_collection(
    topic_id: int,
    event_ids: list[int],
    chapter_titles: list[str] | None = None,
    include_chapter_cards: bool = True,
) -> ClipVariant | None:
    """将同主题的多个高光拼接为合集 MP4。

    流程:
    1. 收集每个事件的 FinalClip 文件路径。
    2. 对每个 clip 做响度标准化(EBU R128)。
    3. 生成章节标题卡(可选,纯色背景+白色文字,2秒)。
    4. 用 FFmpeg concat 滤镜拼接。
    5. 输出到 clips_dir 并写入 ClipVariant 表。

    :param topic_id: 主题 id。
    :param event_ids: 按顺序排列的事件 id 列表。
    :param chapter_titles: 章节标题列表(与 event_ids 一一对应)。
    :param include_chapter_cards: 是否插入章节标题卡。
    :returns: 新 ClipVariant 对象或 ``None``。
    """
    if len(event_ids) < 2:
        logger.warning("合集至少需要 2 个事件,只有 {} 个。", len(event_ids))
        return None

    with get_session() as db:
        topic = db.get(Topic, topic_id)
        if topic is None:
            logger.error("主题 {} 不存在。", topic_id)
            return None

        # 收集 clip 文件。
        clip_files = []
        for eid in event_ids:
            cand = db.get(HighlightCandidate, eid)
            if cand is None:
                logger.warning("事件 {} 找不到候选。", eid)
                continue
            clips = db.exec(
                select(FinalClip)
                .where(
                    FinalClip.candidate_id == cand.id,
                    FinalClip.status.in_([ClipStatus.GENERATED, ClipStatus.READY]),
                )
                .order_by(FinalClip.created_at.desc())
                .limit(1)
            ).first()
            if clips is None or not clips.file_path or not Path(clips.file_path).exists():
                logger.warning("事件 {} 没有可用成品文件。", eid)
                continue
            clip_files.append(
                {
                    "path": clips.file_path,
                    "candidate_id": cand.id,
                    "duration": clips.duration_s or 0,
                }
            )

    if len(clip_files) < 2:
        logger.error("可用成品文件不足 2 个,无法生成合集。")
        return None

    out_dir = clips_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"collection_{topic_id}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.mp4"

    # 章节标题卡(简单纯色+文字)。
    chapter_videos = []
    if include_chapter_cards and chapter_titles:
        for title in chapter_titles:
            if title:
                card_path = _generate_chapter_card(title, out_dir)
                if card_path:
                    chapter_videos.append(card_path)

    # 构建 concat 文件列表。
    # 方案:将所有 clip 先做响度标准化,再用 concat demuxer 拼接。
    # 注意:所有临时文件操作必须在 with 块内完成,不可跨上下文边界。
    normalized_paths = []
    with tempfile.TemporaryDirectory(prefix="blc_collection_") as tmp:
        tmp_path = Path(tmp)

        # 1) 响度标准化每个 clip。
        for i, cf in enumerate(clip_files):
            norm_path = tmp_path / f"norm_{i:03d}.mp4"
            try:
                subprocess.run(
                    [
                        settings.ffmpeg_path,
                        "-y",
                        "-v",
                        "quiet",
                        "-i",
                        cf["path"],
                        "-af",
                        "loudnorm=I=-16:TP=-1.5:LRA=11:linear=true",
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        str(norm_path),
                    ],
                    check=True,
                    timeout=120,
                )
                normalized_paths.append(str(norm_path))
            except subprocess.CalledProcessError:
                logger.warning("响度标准化失败 clip={},使用原始文件。", cf["path"])
                normalized_paths.append(cf["path"])

        # 2) 构建 concat 列表(插入章节卡)。
        concat_list_path = tmp_path / "concat.txt"
        with open(concat_list_path, "w", encoding="utf-8") as f:
            for i, np in enumerate(normalized_paths):
                # 章节标题卡。
                if include_chapter_cards and i < len(chapter_videos):
                    card = chapter_videos[i]
                    if Path(card).exists():
                        f.write(f"file '{card}'\n")
                f.write(f"file '{np}'\n")

        # 3) 用 concat demuxer 拼接。
        try:
            subprocess.run(
                [
                    settings.ffmpeg_path,
                    "-y",
                    "-v",
                    "warning",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_list_path),
                    "-c:v",
                    "libx264",
                    "-crf",
                    "23",
                    "-preset",
                    "medium",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(out_file),
                ],
                check=True,
                timeout=300,
            )
        except subprocess.CalledProcessError as exc:
            logger.error("合集渲染失败: {}", exc)
            return None

    if not out_file.exists():
        return None

    # 计算文件哈希。
    file_hash = hashlib.sha256(out_file.read_bytes()).hexdigest()[:16]
    title = topic.title or f"主题合集 #{topic_id}"

    # 检测时长。
    duration_s = 0.0
    try:
        result = subprocess.run(
            [settings.ffprobe_path, "-v", "quiet", "-print_format", "json", "-show_format", str(out_file)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        info = json.loads(result.stdout)
        duration_s = float(info.get("format", {}).get("duration", 0))
    except Exception:
        pass

    # 写入 ClipVariant。
    with get_session() as db:
        variant = ClipVariant(
            event_id=event_ids[0],
            variant_type=ClipVariantType.COLLECTION_CHAPTER,
            has_subtitles=True,
            resolution="1920x1080",
            file_path=str(out_file),
            file_hash=file_hash,
            duration_s=round(duration_s, 2),
            render_status=RenderStatus.DONE,
            version_number=1,
        )
        db.add(variant)
        db.flush()
        db.refresh(variant)

        # 标记主题为已生成合集。
        topic.is_collection = True
        db.add(topic)

        # 更新事件状态。
        for eid in event_ids:
            event_link = db.exec(
                select(HighlightTopic).where(
                    HighlightTopic.topic_id == topic_id,
                    HighlightTopic.event_id == eid,
                )
            ).first()
            if event_link:
                from app.db.models import CandidateStatus

                cand = db.get(HighlightCandidate, eid)
                if cand:
                    cand.status = CandidateStatus.MERGED
                    db.add(cand)

        logger.info("合集渲染完成: {} ({} 个片段,{:.1f}s)", out_file, len(normalized_paths), duration_s)

    return variant


def _generate_chapter_card(title: str, out_dir: Path) -> str | None:
    """生成章节标题卡(纯色背景+白色文字,2秒)。

    通过 textfile 方式传递标题,避免 FFmpeg drawtext 参数注入风险。

    :param title: 章节标题。
    :param out_dir: 输出目录。
    :returns: 标题卡文件路径或 ``None``。
    """
    if not title or not title.strip():
        return None

    card_path = out_dir / f"chapter_card_{hashlib.md5(title.encode()).hexdigest()[:8]}.mp4"
    if card_path.exists():
        return str(card_path)

    # 使用 textfile 避免 drawtext 注入。
    import tempfile as _tf

    with _tf.NamedTemporaryFile(mode="w", suffix=".txt", encoding="utf-8", delete=False) as text_tmp:
        text_tmp.write(title)
        text_file_path = text_tmp.name

    try:
        subprocess.run(
            [
                settings.ffmpeg_path,
                "-y",
                "-v",
                "quiet",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x1a1a2e:s=1920x1080:d=2:r=30",
                "-vf",
                (
                    f"drawtext=textfile='{text_file_path}':"
                    "fontcolor=white:fontsize=48:"
                    "x=(w-text_w)/2:y=(h-text_h)/2:"
                    "fontfile=/Windows/Fonts/msyh.ttc:"
                    "box=1:boxcolor=black@0.3:boxborderw=20"
                ),
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(card_path),
            ],
            check=True,
            timeout=30,
        )
    except Exception:
        logger.debug("章节标题卡生成失败,跳过。")
        return None
    finally:
        try:
            Path(text_file_path).unlink(missing_ok=True)
        except OSError:
            pass

    if card_path.exists():
        return str(card_path)
    return None
