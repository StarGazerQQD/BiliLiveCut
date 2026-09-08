"""事件与候选 ID 分离、认证冷却和部署配置的真实入口回归。"""

from __future__ import annotations

import base64
import json
import subprocess
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlmodel import select

from app.db.entities import FinalClip, HighlightCandidate, HighlightEvent, HighlightTopic, Topic
from app.db.session import get_session


def authorization(username: str, password: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


@pytest.mark.parametrize(
    "username,password,path",
    [
        ("admin", "中文密码🔒", "/api/settings"),
        ("审核员", "审核口令🌟", "/review/queue"),
    ],
)
async def test_unicode_basic_auth_enters_authorized_page(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, username: str, password: str, path: str
) -> None:
    from app.web import main

    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "中文密码🔒")
    monkeypatch.setattr(main, "_REVIEWER_PASSWORDS", {"审核员": "审核口令🌟"})
    monkeypatch.setattr(main, "_LOGIN_FAILURES", {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1") as client:
        result = await client.get(path, headers=authorization(username, password))
        assert result.status_code == 200
        if username != "admin":
            denied = await client.get("/api/settings", headers=authorization(username, password))
            assert denied.status_code == 403


async def test_correct_password_cannot_bypass_cooldown(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.web import main

    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "correct")
    monkeypatch.setattr(main, "_LOGIN_FAILURES", {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1") as client:
        for _ in range(main._MAX_LOGIN_ATTEMPTS):
            failure = await client.get("/api/settings", headers=authorization("admin", "wrong"))
        assert failure.status_code == 429
        blocked = await client.get("/api/settings", headers=authorization("admin", "correct"))
        assert blocked.status_code == 429
        main._LOGIN_FAILURES["127.0.0.1"] = [main._time.time() - main._LOGIN_WINDOW_S - 1]
        allowed = await client.get("/api/settings", headers=authorization("admin", "correct"))
        assert allowed.status_code == 200


def test_env_example_loads_with_strict_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    example = Path(__file__).resolve().parents[2] / ".env.example"
    target = tmp_path / ".env"
    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    parsed = Settings(_env_file=target)
    assert parsed.room_metadata_refresh_interval_s == 30
    assert "PIP_INDEX_URL=" not in target.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        Settings(_env_file=None, unknown_app_setting=True)


def test_docker_extras_cover_default_asr_and_llm() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dockerfile = (root / "packaging/docker/Dockerfile").read_text(encoding="utf-8")
    extras = dockerfile.split('pip install ".[', 1)[1].split(']"', 1)[0].split(",")
    assert set(extras) <= project["optional-dependencies"].keys()
    assert {"asr-whisper", "asr-funasr", "web", "llm"} <= set(extras)


@pytest.fixture
def topic_media(temp_db: None, tmp_path: Path) -> Path:
    now = datetime.now(UTC)
    with get_session() as db:
        for candidate_id in (1001, 1002, 707, 708):
            db.add(
                HighlightCandidate(
                    id=candidate_id,
                    session_id=1,
                    peak_ts=now,
                    start_ts=now,
                    end_ts=now + timedelta(seconds=30),
                    dedup_hash=f"identity-{candidate_id}",
                    status="approved" if candidate_id > 1000 else "rejected",
                    reason=f"候选 {candidate_id}",
                    features_json=json.dumps({"keyword_hits": ["游戏", "翻盘"]}),
                )
            )
            path = tmp_path / f"candidate-{candidate_id}.mp4"
            path.write_bytes(b"media boundary fixture")
            db.add(FinalClip(candidate_id=candidate_id, file_path=str(path), duration_s=30, status="generated"))
    with get_session() as db:
        for eid, cid in ((707, 1001), (708, 1002)):
            db.add(
                HighlightEvent(
                    id=eid,
                    candidate_id=cid,
                    session_id=1,
                    review_status="approved_collection",
                    asr_text="游戏最后一局精彩翻盘赢下比赛",
                )
            )
        db.add(Topic(id=1, session_id=1, title="已选主题"))
        db.add(Topic(id=2, session_id=2, title="其他场次"))
    with get_session() as db:
        for order, eid in enumerate((707, 708)):
            db.add(HighlightTopic(event_id=eid, topic_id=1, sort_order=order))
    return tmp_path


def test_topic_cluster_links_real_event_ids(topic_media: Path) -> None:
    from app.analysis.topic_cluster import cluster_candidates

    created = cluster_candidates(1)
    assert len(created) == 1
    cluster_candidates(1)
    with get_session() as db:
        links = db.exec(select(HighlightTopic).where(HighlightTopic.topic_id == created[0]["id"])).all()
        assert {link.event_id for link in links} == {707, 708}
        assert len(links) == 2


def test_collection_reads_and_renders_correct_candidates(topic_media: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.pipeline import collection

    events = collection.get_collection_events(1)
    assert [(event["event_id"], event["candidate_id"]) for event in events] == [(707, 1001), (708, 1002)]
    inputs: list[str] = []

    def ffmpeg(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "-af" in command:
            inputs.append(command[command.index("-i") + 1])
        Path(command[-1]).write_bytes(b"rendered result")
        return subprocess.CompletedProcess(command, 0, "", "")

    def ffprobe(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, '{"format":{"duration":"60"}}', "")

    monkeypatch.setattr(collection, "run_cancellable", ffmpeg)
    monkeypatch.setattr(collection.subprocess, "run", ffprobe)
    monkeypatch.setattr(collection, "clips_dir", lambda: topic_media)
    variant = collection.render_collection(1, [708, 707], include_chapter_cards=False)
    assert variant is not None and variant.event_id == 708
    assert [Path(path).name for path in inputs] == ["candidate-1002.mp4", "candidate-1001.mp4"]
    with get_session() as db:
        assert db.get(HighlightCandidate, 1001).status == "merged"
        assert db.get(HighlightCandidate, 1002).status == "merged"
        assert db.get(HighlightCandidate, 707).status == "rejected"


def test_topic_membership_rejects_foreign_or_duplicate_ids(topic_media: Path) -> None:
    from app.analysis.topic_cluster import add_event_to_topic, reorder_topic_events, split_topic
    from app.pipeline.collection import render_collection

    assert not add_event_to_topic(707, 2)
    assert not add_event_to_topic(1001, 1)
    assert not reorder_topic_events(1, [707, 1001])
    assert reorder_topic_events(1, [708, 707])
    assert split_topic(1, [999]) is None
    assert render_collection(1, [707, 707]) is None
    assert render_collection(2, [707, 708]) is None


def test_collection_copy_uses_event_candidate_and_transcript(
    topic_media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.publishing import collection_copywriter as module

    summaries: list[dict[str, object]] = []

    def capture(
        topic_title: str, event_summaries: list[dict[str, object]], total_duration_s: float
    ) -> dict[str, object]:
        summaries.extend(event_summaries)
        return {"title": topic_title, "duration_s": total_duration_s}

    monkeypatch.setattr(module, "generate_copywriter", capture)
    result = module.generate_copywriter_for_topic(1)
    assert result["duration_s"] == 60
    assert [item["candidate_id"] for item in summaries] == [1001, 1002]
    assert all(item["asr_text"] == "游戏最后一局精彩翻盘赢下比赛" for item in summaries)


async def test_collection_page_uses_current_template_api(topic_media: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.web import main

    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1") as client:
        response = await client.get("/collection/1")
        assert response.status_code == 200
        assert "const TOPIC_ID = 1" in response.text
