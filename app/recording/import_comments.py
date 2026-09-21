"""把本地弹幕或字幕文件规范化为相对于视频起点的文本事件。"""

from __future__ import annotations

import html
import io
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

MAX_COMMENT_BYTES = 32 * 1024 * 1024
MAX_EVENTS = 300_000
COMMENT_SUFFIXES = {".xml", ".json", ".srt", ".ass"}


@dataclass(frozen=True)
class CommentEvent:
    """已校验的媒体相对时间与纯文本，不把字幕冒充 ASR 结果。"""

    offset_s: float
    text: str
    user: str | None = None


@dataclass(frozen=True)
class CommentReport:
    """预处理结果及明确丢弃的空白、越界事件数量。"""

    events: list[CommentEvent]
    empty_count: int
    outside_count: int


def _seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("时间必须是秒数或 HH:MM:SS.mmm")
    try:
        if isinstance(value, str) and ":" in value:
            match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d)(?:[.,](\d{1,3}))?", value.strip())
            if not match:
                raise ValueError("时间码无效")
            hours, minutes, seconds, fraction = match.groups()
            result = int(hours) * 3600 + int(minutes) * 60 + int(seconds) + float("0." + (fraction or "0"))
        else:
            result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("时间必须是有限秒数或 HH:MM:SS.mmm") from exc
    if not math.isfinite(result):
        raise ValueError("时间不能是 NaN 或无穷大")
    return result


def _decode(data: bytes) -> str:
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16")
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ValueError("文本编码不支持，请保存为 UTF-8") from exc


def _xml(text: str) -> list[tuple[object, object, object, object]]:
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise ValueError("XML 不允许 DTD 或实体声明")
    rows = []
    depth = 0
    root = None
    try:
        for event, node in ET.iterparse(io.StringIO(text), events=("start", "end")):
            if event == "start":
                depth += 1
                if root is None:
                    root = node
                    if root.tag != "i":
                        raise ValueError('XML 必须采用 B 站 <i><d p="秒数,...">文本</d></i> 格式')
                if depth > 2:
                    raise ValueError("XML 弹幕条目不支持嵌套节点")
                continue
            if node.tag == "d":
                fields = node.get("p", "").split(",")
                _append(rows, (fields[0], None, node.text or "", node.get("user")))
            depth -= 1
            node.clear()
            if root is not None:
                root.clear()
    except ET.ParseError as exc:
        raise ValueError("XML 格式无效") from exc
    return rows


def _append(rows: list[tuple[object, object, object, object]], row: tuple[object, object, object, object]) -> None:
    if len(rows) >= MAX_EVENTS:
        raise ValueError(f"弹幕最多允许 {MAX_EVENTS} 条事件")
    rows.append(row)


def _json(text: str) -> list[tuple[object, object, object, object]]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("JSON 格式无效") from exc
    if isinstance(data, dict):
        keys = [key for key in ("comments", "danmaku", "body") if key in data]
        if len(keys) != 1:
            raise ValueError("JSON 对象必须包含 comments、danmaku 或 body 数组之一")
        data = data[keys[0]]
    if not isinstance(data, list):
        raise ValueError("JSON 必须是事件数组")
    if len(data) > MAX_EVENTS:
        raise ValueError(f"弹幕最多允许 {MAX_EVENTS} 条事件")
    rows = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"JSON 第 {index} 项必须是对象")
        times = [key for key in ("offset_s", "time", "from") if key in item]
        texts = [key for key in ("text", "content") if key in item]
        if len(times) != 1 or len(texts) != 1:
            raise ValueError(f"JSON 第 {index} 项需要唯一的 offset_s/time/from 和 text/content")
        _append(rows, (item[times[0]], item.get("to"), item[texts[0]], item.get("user")))
    return rows


def _srt(text: str) -> list[tuple[object, object, object, object]]:
    rows = []
    for index, block in enumerate(re.split(r"\n\s*\n", text.strip()), 1):
        lines = block.strip().splitlines()
        if lines and lines[0].strip().isdigit():
            lines.pop(0)
        if not lines or "-->" not in lines[0]:
            raise ValueError(f"SRT 第 {index} 段缺少时间范围")
        start, end = lines[0].split("-->", 1)
        _append(rows, (start.strip(), end.strip(), "\n".join(lines[1:]), None))
    return rows


def _ass(text: str) -> list[tuple[object, object, object, object]]:
    rows = []
    fields: list[str] = []
    in_events = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_events = line.casefold() == "[events]"
        if not in_events:
            continue
        key, separator, value = line.partition(":")
        if key.casefold() == "format" and separator:
            fields = [name.strip().casefold() for name in value.split(",")]
            if not {"start", "end", "text"} <= set(fields) or fields[-1] != "text" or len(set(fields)) != len(fields):
                raise ValueError("ASS Events Format 需要 Start、End，且 Text 为最后一列")
        if key.casefold() != "dialogue" or not separator:
            continue
        values = value.split(",", len(fields) - 1) if fields else []
        if not fields or len(values) != len(fields):
            raise ValueError("ASS Dialogue 与 Format 不匹配")
        event = dict(zip(fields, values, strict=True))
        content = event["text"]
        # 矢量绘图不是弹幕文本，不能把坐标送入语义统计。
        if re.search(r"\{[^}]*\\p[1-9]", content):
            content = ""
        content = re.sub(r"\{[^}]*\}", "", content)
        content = content.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
        _append(rows, (event["start"].strip(), event["end"].strip(), content, event.get("name")))
    if not fields:
        raise ValueError("ASS 缺少 Events Format")
    return rows


def preprocess_comments(path: Path, duration_s: float, *, offset_s: float = 0.0) -> CommentReport:
    """限制输入大小并统一格式、时间、文本；保留真实重复弹幕及稳定顺序。"""
    if path.suffix.lower() not in COMMENT_SUFFIXES:
        raise ValueError("弹幕仅支持 XML、JSON、SRT、ASS")
    if not math.isfinite(duration_s) or duration_s <= 0 or not math.isfinite(offset_s):
        raise ValueError("媒体时长或时间偏移无效")
    with path.open("rb") as stream:
        data = stream.read(MAX_COMMENT_BYTES + 1)
    if len(data) > MAX_COMMENT_BYTES:
        raise ValueError("弹幕文件不能超过 32 MiB")
    text = _decode(data).replace("\r\n", "\n").replace("\r", "\n")
    parser = {".xml": _xml, ".json": _json, ".srt": _srt, ".ass": _ass}[path.suffix.lower()]
    rows = parser(text)
    if len(rows) > MAX_EVENTS:
        raise ValueError(f"弹幕最多允许 {MAX_EVENTS} 条事件")
    events: list[CommentEvent] = []
    empty = outside = 0
    for index, (start, end, content, user) in enumerate(rows, 1):
        try:
            point = _seconds(start)
            if end is not None and _seconds(end) < point:
                raise ValueError("结束时间早于开始时间")
            if not isinstance(content, str) or (user is not None and not isinstance(user, str)):
                raise ValueError("文本和用户必须是字符串")
            content = html.unescape(re.sub(r"<[^>]*>", "", content))
            content = " ".join(content.split())
            if len(content) > 4000 or (user is not None and len(user) > 200):
                raise ValueError("单条文本不能超过 4000 字符，用户名不能超过 200 字符")
        except ValueError as exc:
            raise ValueError(f"第 {index} 条事件无效：{exc}") from exc
        point += offset_s
        if not content:
            empty += 1
        elif point < 0 or point >= duration_s:
            outside += 1
        else:
            events.append(CommentEvent(point, content, user.strip() or None if user else None))
    if outside and not events:
        raise ValueError("所有非空事件均在视频时间范围外，请检查时间偏移或所选文件")
    events.sort(key=lambda event: event.offset_s)
    return CommentReport(events, empty, outside)
