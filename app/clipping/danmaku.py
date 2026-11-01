"""把场次普通弹幕转换为有界、无外部指令的剪辑相对 ASS。"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import func
from sqlmodel import Session, select

from app.analysis.source_policy import session_danmaku_lag_s
from app.clipping.models import ClipOptions
from app.db.entities import Danmaku, DanmakuType
from app.db.session import get_session

# 输入按时间稳定排序并限量；每条只读取有限文本，密集场次也不会无限分配内存。
_MAX_EVENTS = 20000
_MAX_TEXT = 120


def _plain_text(value: str | None) -> str:
    if not value:
        return ""
    value = " ".join(value.split())
    value = "".join(char for char in value if not unicodedata.category(char).startswith("C"))
    # ASS 的反斜杠、花括号全部作为普通可见字符显示，禁止覆盖样式、绘图和换行注入。
    value = value.translate(str.maketrans({"\\": "＼", "{": "｛", "}": "｝"}))
    return value if len(value) <= _MAX_TEXT else value[: _MAX_TEXT - 1] + "…"


def _timestamp(seconds: float) -> str:
    ticks = max(0, round(seconds * 100))
    hours, remainder = divmod(ticks, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds_int, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds_int:02d}.{centiseconds:02d}"


def _validate(options: ClipOptions, duration: float, width: int, height: int) -> None:
    """直接传入 ClipOptions 的调用也必须经过与设置中心一致的安全边界。"""
    if not math.isfinite(duration) or not 0 < duration <= 900:
        raise ValueError("弹幕剪辑时长必须在 0~900 秒内")
    if not 1 <= width <= 16384 or not 1 <= height <= 16384:
        raise ValueError("弹幕烧录需要有效的画面分辨率")
    if options.danmaku_mode not in {"scroll", "top", "bottom"}:
        raise ValueError("弹幕模式无效")
    if not 12 <= options.danmaku_font_size <= 120 or not 1 <= options.danmaku_max_lines <= 20:
        raise ValueError("弹幕字号或行数超出范围")
    if re.fullmatch(r"[0-9A-Fa-f]{6}", options.danmaku_color) is None:
        raise ValueError("弹幕颜色必须是六位 RGB 十六进制值")
    if not math.isfinite(options.danmaku_opacity) or not 0 <= options.danmaku_opacity <= 1:
        raise ValueError("弹幕不透明度必须在 0~1 之间")
    if not math.isfinite(options.danmaku_duration_s) or not 2 <= options.danmaku_duration_s <= 20:
        raise ValueError("弹幕显示时长必须在 2~20 秒之间")


def render_danmaku_rows(
    db: Session,
    session_id: int,
    start_ts: datetime,
    duration: float,
    lifetime: float,
) -> tuple[float, list[tuple[datetime, str | None]]]:
    """渲染和恢复指纹共用有界输入，保持同一延迟、窗口及文本截断。"""
    lag = session_danmaku_lag_s(session_id, db)
    if not math.isfinite(lag):
        raise ValueError("场次弹幕延迟必须是有限秒数")
    start = start_ts if start_ts.tzinfo is None else start_ts.astimezone(UTC).replace(tzinfo=None)
    left = start + timedelta(seconds=lag - lifetime)
    right = start + timedelta(seconds=lag + duration)
    rows = db.exec(
        select(Danmaku.ts, func.substr(Danmaku.content, 1, 512))
        .where(
            Danmaku.session_id == session_id,
            Danmaku.msg_type == DanmakuType.DANMAKU,
            Danmaku.ts > left,
            Danmaku.ts < right,
        )
        .order_by(Danmaku.ts, Danmaku.id)
        .limit(_MAX_EVENTS + 1)
    ).all()
    if len(rows) > _MAX_EVENTS:
        logger.warning("弹幕烧录 session={} 窗口超过 {} 条，仅处理最早的普通弹幕", session_id, _MAX_EVENTS)
    return lag, [(timestamp, content) for timestamp, content in rows[:_MAX_EVENTS]]


def write_danmaku_ass(
    session_id: int,
    start_ts: datetime,
    duration: float,
    width: int,
    height: int,
    options: ClipOptions,
    output: Path,
) -> Path | None:
    """生成弹幕侧车；缺失、关闭或透明时返回 None，不改变弹幕采集证据。

    时间使用场次冻结的接收延迟，保留剪辑开始前仍在屏幕内的弹幕及滚动位置。
    同一行直到弹幕离场才复用，满行时舍弃后来的弹幕；始终预留底部 20% 给字幕。
    """
    if not options.danmaku:
        return None
    _validate(options, duration, width, height)
    if options.danmaku_opacity == 0:
        return None
    start = start_ts if start_ts.tzinfo is None else start_ts.astimezone(UTC).replace(tzinfo=None)
    lifetime = options.danmaku_duration_s
    with get_session() as db:
        lag, rows = render_danmaku_rows(db, session_id, start_ts, duration, lifetime)

    # 以 1080 高画面为字号基准，竖屏和其他分辨率同比缩放。
    font_size = options.danmaku_font_size * height / 1080
    line_height = font_size * 1.4
    margin = height * 0.03
    lines = min(options.danmaku_max_lines, max(1, int((height * 0.8 - margin) / line_height)))
    available = [-math.inf] * lines
    color = options.danmaku_color.upper()
    alpha = round((1 - options.danmaku_opacity) * 255)
    primary = f"&H{alpha:02X}{color[4:6]}{color[2:4]}{color[:2]}"
    outline = f"&H{alpha:02X}000000"
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Danmaku,Microsoft YaHei,{font_size:.2f},{primary},{primary},{outline},{outline},"
        "0,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events: list[str] = []
    for received_at, content in rows:
        text = _plain_text(content)
        if not text:
            continue
        at = (received_at - start).total_seconds() - lag
        lane = next((index for index, free_at in enumerate(available) if free_at <= at), None)
        if lane is None:
            continue
        end = min(duration, at + lifetime)
        begin = max(0.0, at)
        if round(begin * 100) >= round(end * 100):
            continue
        available[lane] = at + lifetime
        y = margin + lane * line_height
        if options.danmaku_mode == "scroll":
            # 每字符一个全宽字的保守估计，保证离场前不会在同一行安排下一条。
            text_width = font_size * (len(text) + 1)
            velocity = (width + text_width) / lifetime
            x1 = width - (begin - at) * velocity
            x2 = width - (end - at) * velocity
            tag = f"\\an7\\move({x1:.2f},{y:.2f},{x2:.2f},{y:.2f})"
        else:
            # 固定弹幕保持一行；长文本截断，不遮盖相邻行或 ASR 字幕。
            capacity = max(1, int(width * 0.94 / font_size))
            if len(text) > capacity:
                text = text[: capacity - 1] + "…"
            if options.danmaku_mode == "bottom":
                y = height * 0.8 - (lane + 1) * line_height
            tag = f"\\an8\\pos({width / 2:.2f},{y:.2f})"
        events.append(f"Dialogue: 0,{_timestamp(begin)},{_timestamp(end)},Danmaku,,0,0,0,,{{{tag}\\q2}}{text}\n")
    if not events:
        return None
    output.write_text(header + "".join(events), encoding="utf-8")
    return output
