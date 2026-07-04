"""切片生成与后处理。

把一个高光候选(可能跨多个原始片段)生成为可投稿的 MP4:

1. 选出覆盖候选时间区间的原始片段并用 FFmpeg concat 拼接;
2. 按候选的精确起止时间(含上下文留白)精剪;
3. 后处理:响度标准化 / 去首尾静默 /(可选)竖屏重构 /(可选)烧录字幕;
4. 抽取封面帧;
5. 探测时长/分辨率,计算内容指纹,写入 ``final_clips`` 并更新候选状态。

所有 FFmpeg 参数均在代码中逐项注释说明。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlmodel import select

from app.core.config import settings
from app.core.paths import clips_dir
from app.db.models import (
    CandidateStatus,
    ClipStatus,
    ClipVariant,
    ClipVariantType,
    FinalClip,
    HighlightCandidate,
    RawSegment,
    Transcript,
)
from app.db.session import get_session

# 竖屏目标分辨率(适合手机端短视频)。
_VERT_W, _VERT_H = 1080, 1920


@dataclass(slots=True)
class ClipOptions:
    """切片后处理选项。

    :param loudnorm: 是否做响度标准化。
    :param remove_silence: 是否去除首尾静默。
    :param vertical: 是否竖屏重构。
    :param subtitle: 是否烧录字幕。
    :param max_duration_s: 最大时长(秒)。
    :param crf: x264 质量(0-51)。
    :param preset: x264 编码速度档。
    """

    loudnorm: bool = True
    remove_silence: bool = False
    vertical: bool = False
    subtitle: bool = False
    max_duration_s: int = 180
    crf: int = 20
    preset: str = "veryfast"

    @classmethod
    def from_settings(cls) -> ClipOptions:
        """从全局配置构造默认选项。

        :returns: 依据 ``.env`` 的 :class:`ClipOptions`。
        """
        return cls(
            loudnorm=settings.clip_loudnorm,
            remove_silence=settings.clip_remove_silence,
            vertical=settings.clip_vertical,
            subtitle=settings.clip_subtitle,
            max_duration_s=settings.clip_max_duration_s,
            crf=settings.clip_video_crf,
            preset=settings.clip_preset,
        )


def select_covering_segments(
    session_id: int,
    start_ts: datetime,
    end_ts: datetime,
) -> list[RawSegment]:
    """选出与候选时间区间有重叠的原始片段(按序号排序)。

    :param session_id: 会话 id。
    :param start_ts: 候选起点。
    :param end_ts: 候选终点。
    :returns: 覆盖该区间的片段列表(按 ``seq`` 升序)。
    """
    with get_session() as db:
        rows = db.exec(
            select(RawSegment)
            .where(RawSegment.session_id == session_id)
            .order_by(RawSegment.seq)  # type: ignore[arg-type]
        ).all()
    covering = [
        s
        for s in rows
        if s.start_ts is not None
        and s.end_ts is not None
        and s.end_ts > start_ts
        and s.start_ts < end_ts
    ]
    return covering


def probe_media(path: str) -> tuple[float, int, int]:
    """用 ffprobe 探测媒体时长与分辨率。

    :param path: 媒体文件路径。
    :returns: ``(duration_s, width, height)``;失败时返回 ``(0, 0, 0)``。
    """
    cmd = [
        settings.ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        path,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, check=True, timeout=30).stdout
        data = json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        logger.warning("ffprobe 探测失败 {}: {}", path, exc)
        return 0.0, 0, 0
    duration = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    streams = data.get("streams") or [{}]
    width = int(streams[0].get("width", 0) or 0)
    height = int(streams[0].get("height", 0) or 0)
    return duration, width, height


def _build_audio_filter(options: ClipOptions) -> str:
    """构造音频滤镜链。

    * ``silenceremove``:去掉首尾静默。配合 ``areverse`` 处理尾部。
    * ``loudnorm``:EBU R128 响度标准化(I=-16 LUFS 为流媒体常用目标)。

    :param options: 切片选项。
    :returns: 逗号连接的音频滤镜串;无滤镜时为空字符串。
    """
    filters: list[str] = []
    if options.remove_silence:
        # 去头部静默,再反转去尾部静默,最后反转回来。
        sr = "silenceremove=start_periods=1:start_silence=0.2:start_threshold=-40dB"
        filters += [sr, "areverse", sr, "areverse"]
    if options.loudnorm:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return ",".join(filters)


def _build_video_filter(options: ClipOptions, srt_path: Path | None) -> str:
    """构造视频滤镜链。

    * 竖屏重构:等比缩放到不超过 1080x1920,再用黑边居中填充(避免裁切丢内容)。
    * 字幕:用 ``subtitles`` 滤镜烧录(Windows 下对路径中的冒号做转义)。

    :param options: 切片选项。
    :param srt_path: 字幕文件路径(启用字幕时)。
    :returns: 逗号连接的视频滤镜串;无滤镜时为空字符串。
    """
    filters: list[str] = []
    if options.vertical:
        # decrease 保证不超出目标框;pad 居中补黑边到精确分辨率。
        filters.append(
            f"scale={_VERT_W}:{_VERT_H}:force_original_aspect_ratio=decrease,"
            f"pad={_VERT_W}:{_VERT_H}:(ow-iw)/2:(oh-ih)/2:black"
        )
    if options.subtitle and srt_path is not None:
        escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
        filters.append(f"subtitles='{escaped}'")
    return ",".join(filters)


def _write_concat_list(segments: list[RawSegment], work_dir: Path) -> Path:
    """生成 FFmpeg concat demuxer 所需的清单文件。

    :param segments: 覆盖区间的片段(按序)。
    :param work_dir: 临时工作目录。
    :returns: 清单文件路径。
    """
    list_path = work_dir / "concat.txt"
    lines = []
    for seg in segments:
        # concat demuxer 要求 POSIX 风格路径并对特殊字符转义。
        p = Path(seg.file_path).as_posix()
        p = p.replace("\\", "\\\\").replace("'", "'\\''")
        lines.append(f"file '{p}'")
    list_path.write_text("\n".join(lines), encoding="utf-8")
    return list_path


def _render_intro_outro_cards(
    candidate_id: int,
    work_dir: Path,
    options: ClipOptions,
    width: int = 1920,
    height: int = 1080,
) -> list[Path]:
    """渲染片头/片尾标题卡视频片段(V0.1.8 P1.2)。

    从数据库加载片头/片尾模板,用模板变量替换后,
    通过 FFmpeg 生成带文字的视频卡。

    :param candidate_id: 候选 ID。
    :param work_dir: 临时工作目录。
    :param options: ClipOptions。
    :param width: 视频宽。
    :param height: 视频高。
    :returns: 按顺序排列的卡片文件列表[]。
    """
    from datetime import date

    from app.db.models import HighlightCandidate, IntroTemplate, LiveRoom, RecordingSession

    cards: list[Path] = []

    # 查找默认模板。
    with get_session() as db:
        tmpl = db.exec(
            select(IntroTemplate).where(IntroTemplate.is_default == True)  # noqa: E712
        ).first()
        if tmpl is None:
            tmpl = db.exec(select(IntroTemplate)).first()
        if tmpl is None:
            return cards

        # 构建模板变量。
        cand = db.get(HighlightCandidate, candidate_id)
        vars_dict: dict[str, str] = {
            "date": date.today().isoformat(),
            "time": "",
            "streamer_name": "",
            "game_name": "",
            "room_title": "",
        }
        if cand and cand.session_id:
            session = db.get(RecordingSession, cand.session_id)
            if session and session.room_id:
                room = db.get(LiveRoom, session.room_id)
                if room:
                    vars_dict["streamer_name"] = room.uploader_name or ""
                    vars_dict["game_name"] = ""
                    vars_dict["room_title"] = room.title or ""
        if cand:
            vars_dict["time"] = cand.start_ts.strftime("%H:%M") if cand.start_ts else ""

    # 生成片头。
    if tmpl.intro_enabled and tmpl.intro_text:
        text = _resolve_variables(tmpl.intro_text, vars_dict)
        card_path = work_dir / "intro_card.mp4"
        _render_text_card(card_path, text, tmpl.intro_duration_s,
                         tmpl.intro_font_name, tmpl.intro_font_size,
                         tmpl.intro_font_color, tmpl.intro_bg_color,
                         width, height)
        cards.append(card_path)

    # 片尾稍后在主视频后添加(通过修改 concat pipeline 实现)。
    return cards


def _render_text_card(
    out_path: Path,
    text: str,
    duration_s: float,
    font_name: str,
    font_size: int,
    font_color: str,
    bg_color: str,
    width: int,
    height: int,
) -> None:
    """用 FFmpeg color 源 + drawtext 生成文字标题卡视频。

    :param out_path: 输出文件路径。
    :param text: 显示文字。
    :param duration_s: 时长(秒)。
    :param font_name: 字体名。
    :param font_size: 字号。
    :param font_color: 文字颜色。
    :param bg_color: 背景颜色(支持透明度如 black@0.6)。
    :param width: 视频宽。
    :param height: 视频高。
    """
    import re as _re
    import tempfile as _tmp

    # C4:清洗 FFmpeg drawtext 参数,防止滤镜注入。
    safe_font = _re.sub(r"[':,;\\]", "", font_name or "sans")
    safe_font_color = font_color if _re.fullmatch(r"#?[0-9a-fA-F]{3,8}", font_color or "") else "white"
    safe_bg_color = bg_color if _re.fullmatch(r"#?[0-9a-fA-F]{3,8}", bg_color or "") else "black"

    tf = _tmp.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    tf.write(text)
    tf.close()

    cmd = [
        settings.ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={safe_bg_color}:s={width}x{height}:d={duration_s}",
        "-vf", (
            f"drawtext=textfile='{tf.name}':"
            f"font='{safe_font}':"
            f"fontcolor={safe_font_color}:fontsize={font_size}:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:"
            f"box=1:boxcolor=black@0.4:boxborderw=20"
        ),
        "-c:v", "libx264", "-crf", "18", "-preset", "ultrafast",
        "-c:a", "an",
        "-y", str(out_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        logger.info("标题卡已生成: {} ({}s)", out_path.name, duration_s)
    except subprocess.CalledProcessError as exc:
        logger.warning("标题卡渲染失败: {}", exc.stderr.decode("utf-8", errors="ignore"))
    finally:
        import os
        try:
            os.unlink(tf.name)
        except OSError:
            pass


def _resolve_variables(text: str, vars_dict: dict[str, str]) -> str:
    """替换模板变量。

    :param text: 含 ``{key}`` 占位符的文本。
    :param vars_dict: 变量名->值映射。
    :returns: 替换后的文本。
    """
    import re
    def _repl(m: re.Match[str]) -> str:
        return vars_dict.get(m.group(1), m.group(0))
    return re.sub(r"\{(\w+)\}", _repl, text)


def _build_srt(segments: list[RawSegment], cut_offset: float, duration: float) -> str:
    """从覆盖片段的转写词级时间戳构造剪辑相对时间轴的 SRT 字幕。

    片段内时间戳相对各自起点(录制时 reset),需按片段在拼接流中的累计偏移换算,
    再减去剪辑起点偏移,落在 ``[0, duration]`` 的词才保留。

    :param segments: 覆盖片段(按序)。
    :param cut_offset: 剪辑起点相对拼接流起点的偏移(秒)。
    :param duration: 剪辑时长(秒)。
    :returns: SRT 文本(可能为空)。
    """
    seg_ids = [s.id for s in segments if s.id is not None]
    with get_session() as db:
        rows = db.exec(select(Transcript).where(Transcript.segment_id.in_(seg_ids))).all()  # type: ignore[attr-defined]
    by_seg = {t.segment_id: t for t in rows}

    entries: list[tuple[float, float, str]] = []
    cumulative = 0.0
    for seg in segments:
        t = by_seg.get(seg.id)
        if t and t.words_json:
            for w in json.loads(t.words_json):
                start = cumulative + float(w["start"]) - cut_offset
                end = cumulative + float(w["end"]) - cut_offset
                if end < 0 or start > duration:
                    continue
                entries.append((max(0.0, start), min(duration, end), str(w["w"]).strip()))
        cumulative += seg.duration_s or float(settings.segment_duration_s)

    # V0.1.8 P1.3:加载字幕模板断句配置。
    max_chars = 14
    min_ms = 800
    max_ms = 5000
    line_gap = 200
    with get_session() as db:
        from app.db.models import SubtitleTemplate
        tmpl = db.exec(
            select(SubtitleTemplate).where(SubtitleTemplate.is_default == True)  # noqa: E712
        ).first()
        if tmpl:
            max_chars = tmpl.max_chars_per_line
            min_ms = tmpl.min_display_ms
            max_ms = tmpl.max_display_ms
            line_gap = tmpl.line_gap_ms

    # 把词聚合成短句字幕(每约 N 个字或遇到停顿断行)。
    return _group_srt(entries, max_chars=max_chars, min_display_ms=min_ms,
                      max_display_ms=max_ms, line_gap_ms=line_gap)


def _group_srt(
    words: list[tuple[float, float, str]],
    max_chars: int = 14,
    min_display_ms: int = 800,
    max_display_ms: int = 5000,
    line_gap_ms: int = 200,
) -> str:
    """把词级条目聚合成 SRT 字幕块。

    :param words: ``(start, end, text)`` 列表。
    :param max_chars: 每条字幕最大字符数。
    :param min_display_ms: 最短显示时长(毫秒)。
    :param max_display_ms: 最长显示时长(毫秒)。
    :param line_gap_ms: 行间间隔(毫秒)。
    :returns: SRT 文本。
    """
    if not words:
        return ""
    blocks: list[tuple[float, float, str]] = []
    cur_text = ""
    cur_start = words[0][0]
    cur_end = words[0][1]
    for start, end, text in words:
        if cur_text and len(cur_text) + len(text) > max_chars:
            blocks.append((cur_start, cur_end, cur_text))
            cur_text, cur_start = "", start
        cur_text += text
        cur_end = end
    if cur_text:
        blocks.append((cur_start, cur_end, cur_text))

    def fmt(t: float) -> str:
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        ms = int((s - int(s)) * 1000)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"

    lines = []
    for i, (start, end, text) in enumerate(blocks, 1):
        # V0.1.8 P1.3:应用最短/最长显示时长。
        dur_ms = (end - start) * 1000
        if dur_ms < min_display_ms:
            end = start + min_display_ms / 1000
        elif dur_ms > max_display_ms:
            end = start + max_display_ms / 1000
        lines.append(f"{i}\n{fmt(start)} --> {fmt(end)}\n{text}\n")
    return "\n".join(lines)


def produce_clip(candidate_id: int, options: ClipOptions | None = None) -> FinalClip:
    """把一个高光候选生成为成品 MP4 并入库。

    :param candidate_id: ``highlight_candidates`` 主键。
    :param options: 切片选项;默认取自配置。
    :returns: 新建的 :class:`FinalClip`。
    :raises ValueError: 候选不存在或找不到覆盖片段时。
    :raises RuntimeError: FFmpeg 执行失败时。
    """
    options = options or ClipOptions.from_settings()

    with get_session() as db:
        cand = db.get(HighlightCandidate, candidate_id)
        if cand is None:
            raise ValueError(f"候选不存在: id={candidate_id}")
        session_id = cand.session_id
        start_ts = cand.start_ts
        end_ts = cand.end_ts
        peak_ts = cand.peak_ts

    segments = select_covering_segments(session_id, start_ts, end_ts)
    if not segments:
        raise ValueError(f"候选 {candidate_id} 找不到覆盖的原始片段。")

    base_ts = segments[0].start_ts
    if base_ts is None:
        raise ValueError(f"原始片段 {segments[0].id} 缺少 start_ts,无法计算裁剪偏移。")
    cut_offset = max(0.0, (start_ts - base_ts).total_seconds())
    raw_duration = (end_ts - start_ts).total_seconds()
    duration = max(2.0, min(raw_duration, float(options.max_duration_s)))
    peak_rel = max(0.0, (peak_ts - start_ts).total_seconds())

    out_path = clips_dir() / f"clip_{candidate_id}.mp4"
    cover_path = clips_dir() / f"clip_{candidate_id}.jpg"

    with tempfile.TemporaryDirectory(prefix="blc_clip_") as tmp:
        work_dir = Path(tmp)
        concat_list = _write_concat_list(segments, work_dir)

        # V0.1.8 P1.2:生成片头/片尾标题卡。
        intro_cards = _render_intro_outro_cards(candidate_id, work_dir, options)
        if intro_cards:
            # 将片头卡片插入 concat 列表最前面。
            card_lines = [f"file '{card.as_posix()}'" for card in intro_cards]
            existing = concat_list.read_text(encoding="utf-8")
            new_concat = work_dir / "concat_with_intro.txt"
            new_concat.write_text("\n".join(card_lines) + "\n" + existing, encoding="utf-8")
            concat_list = new_concat
            logger.info("片头卡片已注入 concat,共 {} 段", len(intro_cards))

        srt_path: Path | None = None
        if options.subtitle:
            srt_text = _build_srt(segments, cut_offset, duration)
            if srt_text:
                srt_path = work_dir / "sub.srt"
                srt_path.write_text(srt_text, encoding="utf-8")

        _run_ffmpeg_clip(concat_list, out_path, cut_offset, duration, options, srt_path)

    real_duration, width, height = probe_media(str(out_path))
    try:
        _grab_cover(out_path, cover_path, min(peak_rel, max(0.5, real_duration / 2)))
    except Exception as exc:  # noqa: BLE001 — 封面抽取失败不影响切片主体
        logger.warning("封面抽帧异常(不影响切片): {}", exc)
    content_hash = _file_sha1(out_path)

    clip = FinalClip(
        candidate_id=candidate_id,
        file_path=str(out_path),
        cover_path=str(cover_path) if cover_path.exists() else None,
        duration_s=real_duration,
        width=width or (_VERT_W if options.vertical else None),
        height=height or (_VERT_H if options.vertical else None),
        content_hash=content_hash,
        status=ClipStatus.GENERATED,
    )
    with get_session() as db:
        db.add(clip)
        db.flush()
        db.refresh(clip)
        cand = db.get(HighlightCandidate, candidate_id)
        if cand is not None:
            cand.status = CandidateStatus.CLIPPED
            db.add(cand)
        clip_id = clip.id

    logger.success(
        "切片完成 clip={} candidate={} 时长={:.1f}s 分辨率={}x{} -> {}",
        clip_id,
        candidate_id,
        real_duration,
        width,
        height,
        out_path.name,
    )

    # V0.1.8: 生成多版本 ClipVariant 记录。
    _create_clip_variants(clip, options, segments, cut_offset)

    # V0.1.8 P1.1: 渲染多版本出片(归档版+压制版+互补字幕版)。
    _render_variants(clip, options, concat_list, cut_offset, duration, srt_path)

    # V0.1.8 P2:切片完成通知。
    from app.notify.webhook import notify_clip_complete
    notify_clip_complete(candidate_id, str(out_path), real_duration)

    return clip


def _create_clip_variants(
    clip: FinalClip,
    options: ClipOptions,
    segments: list[RawSegment],
    cut_offset: float,
) -> None:
    """为成品切片创建多版本 ClipVariant 记录。

    规则:
    - 主版本记作 SINGLE(已渲染,直接关联)。
    - 如果渲染了字幕,同时记作 SUBTITLED。
    - 净版(NO_SUBTITLES)在渲染时未生成字幕则自动记。
    - 压制版(COMPRESSED)和归档版(ARCHIVE):先标记 queued,P1.1 渲染流程更新。

    :param clip: 新创建的 FinalClip。
    :param options: 渲染选项。
    :param segments: 覆盖片段列表。
    :param cut_offset: 裁剪偏移。
    """
    with get_session() as db:
        # SINGLE:主版本(总是生成,已渲染完成)。
        db.add(ClipVariant(
            event_id=clip.candidate_id,
            variant_type=ClipVariantType.SINGLE,
            file_path=clip.file_path,
            duration_s=clip.duration_s,
            resolution=f"{clip.width}x{clip.height}" if clip.width and clip.height else None,
            has_subtitles=options.subtitle,
            render_status="completed",
            version_number=1,
            created_at=clip.created_at,
        ))
        # 按字幕状态标记。
        if options.subtitle:
            db.add(ClipVariant(
                event_id=clip.candidate_id,
                variant_type=ClipVariantType.SUBTITLED,
                file_path=clip.file_path,
                duration_s=clip.duration_s,
                has_subtitles=True,
                render_status="completed",
                version_number=1,
                created_at=clip.created_at,
            ))
        else:
            db.add(ClipVariant(
                event_id=clip.candidate_id,
                variant_type=ClipVariantType.NO_SUBTITLES,
                file_path=clip.file_path,
                duration_s=clip.duration_s,
                has_subtitles=False,
                render_status="completed",
                version_number=1,
                created_at=clip.created_at,
            ))
        # ARCHIVE + COMPRESSED:先标记 queued,由 _render_variants 完成后更新。
        db.add(ClipVariant(
            event_id=clip.candidate_id,
            variant_type=ClipVariantType.ARCHIVE,
            has_subtitles=options.subtitle,
            render_status="queued",
            version_number=1,
        ))
        db.add(ClipVariant(
            event_id=clip.candidate_id,
            variant_type=ClipVariantType.COMPRESSED,
            has_subtitles=options.subtitle,
            render_status="queued",
            version_number=1,
        ))
        logger.info(
            "ClipVariant 队列已创建 candidate={} variants={}",
            clip.candidate_id,
            "SUBTITLED" if options.subtitle else "NO_SUBTITLES",
        )


def _run_ffmpeg_clip(
    concat_list: Path,
    out_path: Path,
    cut_offset: float,
    duration: float,
    options: ClipOptions,
    srt_path: Path | None,
) -> None:
    """执行精剪 + 后处理的 FFmpeg 命令。

    参数说明:

    * ``-f concat -safe 0 -i list``:用 concat demuxer 把多个 ts 当作单一输入;
    * ``-ss`` 置于输入后:对拼接流做帧精确定位(再编码,慢但准);
    * ``-t duration``:截取时长;
    * ``-af`` / ``-vf``:音/视频后处理滤镜(见各自构造函数);
    * ``-c:v libx264 -crf -preset``:H.264 编码,CRF 控质量;
    * ``-c:a aac -b:a 160k``:AAC 音频;
    * ``-movflags +faststart``:moov 前置,便于网络边下边播。

    :param concat_list: concat 清单文件。
    :param out_path: 输出 MP4 路径。
    :param cut_offset: 起点偏移(秒)。
    :param duration: 时长(秒)。
    :param options: 切片选项。
    :param srt_path: 字幕文件(可空)。
    :raises RuntimeError: FFmpeg 失败时。
    """
    af = _build_audio_filter(options)
    vf = _build_video_filter(options, srt_path)

    cmd = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-ss",
        f"{cut_offset:.3f}",
        "-t",
        f"{duration:.3f}",
    ]
    if af:
        cmd += ["-af", af]
    if vf:
        cmd += ["-vf", vf]
    cmd += [
        "-c:v",
        "libx264",
        "-crf",
        str(options.crf),
        "-preset",
        options.preset,
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        "-y",
        str(out_path),
    ]

    logger.debug("切片 FFmpeg 命令: {}", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, timeout=600)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="ignore")
        raise RuntimeError(f"FFmpeg 切片失败: {stderr}")


def _grab_cover(video_path: Path, cover_path: Path, at_s: float) -> None:
    """从成品视频抽取一帧作为封面建议。

    :param video_path: 视频路径。
    :param cover_path: 输出封面路径。
    :param at_s: 抽帧时间点(秒)。
    """
    cmd = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{at_s:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-y",
        str(cover_path),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    if result.returncode != 0:
        logger.warning("封面抽帧失败: {}", result.stderr.decode("utf-8", errors="ignore"))


def _render_variants(
    clip: FinalClip,
    options: ClipOptions,
    concat_list: Path,
    cut_offset: float,
    duration: float,
    srt_path: Path | None,
) -> None:
    """渲染多版本出片(V0.1.8 P1.1)。

    在主干视频生成后,异步渲染:
    - ARCHIVE:高码率归档版(CRF 14,preset medium,256k 音频)
    - COMPRESSED:投稿压制版(CRF 26,preset veryfast,128k 音频)
    - 互补字幕版:与主干相反(主干有字幕→净版,主干无字幕→带字幕版)

    每个变体渲染完成即更新对应的 ClipVariant 记录。

    :param clip: 主干 FinalClip。
    :param options: 主干渲染选项。
    :param concat_list: concat 清单文件。
    :param cut_offset: 裁剪偏移(秒)。
    :param duration: 时长(秒)。
    :param srt_path: 字幕文件(可空)。
    """
    variants_dir = Path(clip.file_path).parent

    # --- ARCHIVE:高码率归档 ---
    archive_path = variants_dir / f"clip_{clip.candidate_id}_archive.mp4"
    _render_single_variant(
        concat_list, archive_path, cut_offset, duration,
        crf=14, preset="medium", audio_bitrate="256k",
        subtitle=options.subtitle, srt_path=srt_path,
        variant_type=ClipVariantType.ARCHIVE, clip=clip,
    )

    # --- COMPRESSED:投稿压制 ---
    compressed_path = variants_dir / f"clip_{clip.candidate_id}_compressed.mp4"
    _render_single_variant(
        concat_list, compressed_path, cut_offset, duration,
        crf=26, preset="veryfast", audio_bitrate="128k",
        subtitle=options.subtitle, srt_path=srt_path,
        variant_type=ClipVariantType.COMPRESSED, clip=clip,
    )

    # --- 互补字幕版 ---
    counterpart_subtitle = not options.subtitle
    if counterpart_subtitle:
        # 主干无字幕 → 渲染带字幕版:用候选关联的 session 查找 segments 构建 SRT。
        counterpart_srt: Path | None = None
        from app.db.models import HighlightCandidate as HC
        with get_session() as db:
            cand = db.get(HC, clip.candidate_id)
            if cand:
                segs = db.exec(
                    select(RawSegment)
                    .where(RawSegment.session_id == cand.session_id)
                    .order_by(RawSegment.seq)
                ).all()
                srt_text = _build_srt(list(segs), cut_offset, duration)
                if srt_text:
                    counterpart_srt = variants_dir / f"clip_{clip.candidate_id}_sub.srt"
                    counterpart_srt.write_text(srt_text, encoding="utf-8")
        sub_path = variants_dir / f"clip_{clip.candidate_id}_subtitled.mp4"
        _render_single_variant(
            concat_list, sub_path, cut_offset, duration,
            crf=options.crf, preset=options.preset, audio_bitrate="160k",
            subtitle=True, srt_path=counterpart_srt,
            variant_type=ClipVariantType.SUBTITLED, clip=clip,
        )
        # 清理临时字幕
        if counterpart_srt and counterpart_srt.exists():
            try:
                counterpart_srt.unlink()
            except OSError:
                pass
    else:
        # 主干有字幕 → 渲染无字幕净版
        clean_path = variants_dir / f"clip_{clip.candidate_id}_clean.mp4"
        _render_single_variant(
            concat_list, clean_path, cut_offset, duration,
            crf=options.crf, preset=options.preset, audio_bitrate="160k",
            subtitle=False, srt_path=None,
            variant_type=ClipVariantType.NO_SUBTITLES, clip=clip,
        )


def _render_single_variant(
    concat_list: Path,
    out_path: Path,
    cut_offset: float,
    duration: float,
    *,
    crf: int,
    preset: str,
    audio_bitrate: str,
    subtitle: bool,
    srt_path: Path | None,
    variant_type: str,
    clip: FinalClip,
) -> None:
    """执行单个变体的 FFmpeg 渲染并更新数据库。

    :param concat_list: concat 清单文件。
    :param out_path: 输出文件路径。
    :param cut_offset: 裁剪偏移(秒)。
    :param duration: 时长(秒)。
    :param crf: x264 CRF 值。
    :param preset: x264 preset。
    :param audio_bitrate: 音频码率。
    :param subtitle: 是否烧录字幕。
    :param srt_path: SRT 字幕路径(可空)。
    :param variant_type: 变体类型。
    :param clip: 关联的 FinalClip。
    """
    # 构建简化的 ClipOptions 用于滤镜构建
    from dataclasses import replace

    var_opts = replace(
        ClipOptions.from_settings(),
        crf=crf,
        preset=preset,
        subtitle=subtitle,
    )

    af = _build_audio_filter(var_opts)
    vf = _build_video_filter(var_opts, srt_path)

    cmd = [
        settings.ffmpeg_path,
        "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-ss", f"{cut_offset:.3f}",
        "-t", f"{duration:.3f}",
    ]
    if af:
        cmd += ["-af", af]
    if vf:
        cmd += ["-vf", vf]
    cmd += [
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-movflags", "+faststart", "-y", str(out_path),
    ]

    logger.info("渲染变体 {} {} -> {} (CRF={} preset={})", variant_type, clip.candidate_id, out_path.name, crf, preset)
    result = subprocess.run(cmd, capture_output=True, timeout=1800)

    with get_session() as db:
        variant = db.exec(
            select(ClipVariant).where(
                ClipVariant.event_id == clip.candidate_id,
                ClipVariant.variant_type == variant_type,
            )
        ).first()
        if variant:
            if result.returncode == 0:
                real_dur, w, h = probe_media(str(out_path))
                variant.file_path = str(out_path)
                variant.duration_s = real_dur or duration
                variant.resolution = f"{w}x{h}" if w and h else None
                variant.has_subtitles = subtitle
                variant.render_status = "completed"
                logger.success("变体 {} candidate={} 渲染完成 -> {}", variant_type, clip.candidate_id, out_path.name)
            else:
                variant.render_status = "failed"
                stderr = result.stderr.decode("utf-8", errors="ignore")
                logger.error("变体 {} candidate={} 渲染失败: {}", variant_type, clip.candidate_id, stderr)
            db.add(variant)
            db.commit()


def _file_sha1(path: Path) -> str:
    """计算文件 SHA1,用于成品查重。

    :param path: 文件路径。
    :returns: 十六进制 SHA1 摘要。
    """
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
