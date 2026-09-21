"""本地弹幕和字幕预处理的格式、时间轴与资源边界。"""

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from app.recording.import_comments import preprocess_comments


@pytest.mark.parametrize(
    ("suffix", "text"),
    [
        ("xml", '<i><d p="1.25,1,25,16777215,0,0,u,1">你好 &amp; 世界</d></i>'),
        ("json", '[{"offset_s":1.25,"text":"你好 &amp; 世界"}]'),
        ("json", '{"body":[{"from":1.25,"to":2,"content":"你好 &amp; 世界"}]}'),
        ("srt", "1\r\n00:00:01,250 --> 00:00:02,000\r\n<b>你好</b> &amp; 世界\r\n"),
        (
            "ass",
            "[Events]\nFormat: Layer, Start, End, Style, Name, Text\n"
            "Dialogue: 0,0:00:01.25,0:00:02.00,Default,,{\\b1}你好 &amp; 世界",
        ),
    ],
)
def test_formats_share_media_relative_timeline(tmp_path: Path, suffix: str, text: str) -> None:
    path = tmp_path / f"comments.{suffix}"
    path.write_text(text, encoding="utf-8-sig", newline="")
    report = preprocess_comments(path, 20, offset_s=2)
    assert [(event.offset_s, event.text) for event in report.events] == [(3.25, "你好 & 世界")]
    assert report.empty_count == report.outside_count == 0


def test_sort_preserves_duplicates_and_reports_dropped_rows(tmp_path: Path) -> None:
    path = tmp_path / "comments.json"
    path.write_text(
        '[{"time":3,"text":"666","user":"a"},{"time":1,"text":"好"},'
        '{"time":3,"text":"666","user":"b"},{"time":-1,"text":"越界"},'
        '{"time":10,"text":"边界"},{"time":2,"text":"  "}]',
        encoding="utf-8",
    )
    report = preprocess_comments(path, 10)
    assert [event.offset_s for event in report.events] == [1, 3, 3]
    assert [event.user for event in report.events[1:]] == ["a", "b"]
    assert report.outside_count == 2
    assert report.empty_count == 1


def test_ass_text_commas_multiline_and_drawings(tmp_path: Path) -> None:
    path = tmp_path / "events.ass"
    path.write_text(
        "[Events]\nFormat: Start, End, Text\n"
        "Dialogue: 0:00:01.00,0:00:02.00,{\\pos(3,4)}甲,乙\\N丙\\h丁\n"
        "Comment: 0:00:01.00,0:00:02.00,不应读取\n"
        "Dialogue: 0:00:03.00,0:00:04.00,{\\p1}m 0 0 l 3 4",
        encoding="gb18030",
    )
    report = preprocess_comments(path, 10)
    assert report.events[0].text == "甲,乙 丙 丁"
    assert report.empty_count == 1


@pytest.mark.parametrize(
    ("suffix", "text"),
    [
        ("xml", '<!DOCTYPE i [<!ENTITY a "attack">]><i><d p="1">&a;</d></i>'),
        ("xml", "<i>"),
        ("xml", "<not_bilibili />"),
        ("json", '[{"time":NaN,"text":"x"}]'),
        ("json", '[{"time":true,"text":"x"}]'),
        ("json", '[{"time":1,"text":2}]'),
        ("json", '[{"time":1,"offset_s":2,"text":"x"}]'),
        ("json", '{"comments":{},"body":[]}'),
        ("json", '[{"time":100,"text":"x"}]'),
        ("srt", "1\n00:00:03,000 --> 00:00:02,000\nx"),
        ("srt", "1\n00:61:00,000 --> 00:61:01,000\nx"),
        ("ass", "[Events]\nDialogue: 0,1,2,x"),
        ("ass", "[Events]\nFormat: Start, Text, End\nDialogue: 1,x,2"),
    ],
)
def test_malformed_or_ambiguous_events_are_rejected(tmp_path: Path, suffix: str, text: str) -> None:
    path = tmp_path / f"comments.{suffix}"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        preprocess_comments(path, 10)


def test_size_and_event_limits_are_enforced(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    import app.recording.import_comments as module

    path = tmp_path / "events.json"
    path.write_text('[{"time":1,"text":"x"},{"time":2,"text":"y"}]', encoding="utf-8")
    monkeypatch.setattr(module, "MAX_EVENTS", 1)
    with pytest.raises(ValueError, match="事件"):
        preprocess_comments(path, 10)
    monkeypatch.setattr(module, "MAX_COMMENT_BYTES", 10)
    with pytest.raises(ValueError, match="32 MiB"):
        preprocess_comments(path, 10)


def test_empty_xml_is_valid_and_utf16_is_supported(tmp_path: Path) -> None:
    path = tmp_path / "events.xml"
    path.write_text("<i />", encoding="utf-16")
    assert not preprocess_comments(path, 10).events
