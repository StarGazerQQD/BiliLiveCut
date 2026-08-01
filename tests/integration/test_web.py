"""Web 控制台 API 测试(FastAPI TestClient,不发起真实网络/录制)。"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def test_dashboard_and_room_crud(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """概览、添加直播间(mock 取流)、调阈值等核心 API 正常工作。"""
    from app.sources.bilibili.client import BilibiliLiveClient, RoomInfo
    from app.web.main import app

    async def fake_room_info(self: BilibiliLiveClient, url: str) -> RoomInfo:  # noqa: ANN001
        return RoomInfo(room_id=12345, short_id=0, uid=1, live_status=0)

    monkeypatch.setattr(BilibiliLiveClient, "get_room_info", fake_room_info)

    with TestClient(app) as client:
        # 初始概览
        r = client.get("/api/dashboard")
        assert r.status_code == 200
        assert r.json()["counts"]["candidates"] == 0

        # 添加直播间(已授权)
        r = client.post(
            "/api/rooms",
            json={"url": "https://live.bilibili.com/12345", "authorized": True},
        )
        assert r.status_code == 200
        db_id = r.json()["id"]
        assert r.json()["room_id"] == 12345

        # 出现在概览中
        rooms = client.get("/api/dashboard").json()["rooms"]
        assert any(rm["id"] == db_id for rm in rooms)

        # 调整阈值与模式
        r = client.patch(f"/api/rooms/{db_id}", json={"mode": "auto", "highlight_threshold": 0.7})
        assert r.status_code == 200
        r = client.patch(
            f"/api/rooms/{db_id}",
            json={"room_config": {"highlight_scorer_mode": "shadow"}},
        )
        assert r.status_code == 200
        room_payload = next(item for item in client.get("/api/dashboard").json()["rooms"] if item["id"] == db_id)
        assert room_payload["room_config"]["highlight_scorer_mode"] == "shadow"
        assert r.json()["mode"] == "auto"
        assert abs(r.json()["highlight_threshold"] - 0.7) < 1e-6


def test_room_pipeline_switches_are_independently_configurable(temp_db: None) -> None:
    """Portable Web API 应完整暴露五个房间级流水线开关。"""
    from app.db.models import LiveRoom
    from app.db.session import get_session
    from app.web.main import app

    with get_session() as db:
        room = LiveRoom(input_url="switches", room_id=23456, authorized=True)
        db.add(room)
        db.flush()
        room_id = room.id
    assert room_id is not None

    with TestClient(app) as client:
        response = client.patch(
            f"/api/rooms/{room_id}",
            json={
                "auto_record": True,
                "auto_analyze": True,
                "auto_render": True,
                "auto_approve": True,
                "auto_upload": True,
                "auto_approve_threshold": 0.88,
                "review_threshold": 0.56,
            },
        )

    assert response.status_code == 200
    with get_session() as db:
        updated = db.get(LiveRoom, room_id)
        assert updated is not None
        assert updated.auto_record is True
        assert updated.auto_analyze is True
        assert updated.auto_render is True
        assert updated.auto_approve is True
        assert updated.auto_upload is True
        assert updated.auto_approve_threshold == pytest.approx(0.88)
        assert updated.review_threshold == pytest.approx(0.56)


def test_room_patch_distinguishes_missing_room_and_recording_conflict(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    """录制中锁定字段返回冲突，只有不存在的直播间返回 404。"""
    from app.db.models import LiveRoom
    from app.db.session import get_session
    from app.web import service
    from app.web.main import app

    with get_session() as db:
        room = LiveRoom(input_url="recording-room", room_id=34567, authorized=True)
        db.add(room)
        db.flush()
        room_id = room.id
    assert room_id is not None

    def fake_is_running(_db_id: int) -> bool:
        return True

    monkeypatch.setattr(service.recorder_manager, "is_running", fake_is_running)

    with TestClient(app) as client:
        conflict = client.patch(f"/api/rooms/{room_id}", json={"schedule_enabled": True})
        allowed = client.patch(f"/api/rooms/{room_id}", json={"highlight_threshold": 0.72})
        invalid = client.patch(
            f"/api/rooms/{room_id}",
            json={"room_config": {"highlight_scorer_mode": "invalid"}},
        )
        missing = client.patch("/api/rooms/999999", json={"highlight_threshold": 0.72})

    assert conflict.status_code == 409
    assert "正在录制" in conflict.json()["detail"]
    assert allowed.status_code == 200
    assert invalid.status_code == 400
    assert missing.status_code == 404
    assert "房间不存在" in missing.json()["detail"]


def test_add_room_requires_authorization(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """未确认授权时添加直播间返回 400。"""
    from app.web.main import app

    with TestClient(app) as client:
        r = client.post("/api/rooms", json={"url": "123", "authorized": False})
        assert r.status_code == 400


def test_start_unauthorized_room_returns_400(temp_db: None) -> None:
    """对未授权房间启动录制应被拒绝(400)。"""
    from app.db.models import LiveRoom
    from app.db.session import get_session
    from app.web.main import app

    with get_session() as db:
        room = LiveRoom(input_url="x", room_id=1, authorized=False)
        db.add(room)
        db.flush()
        rid = room.id

    with TestClient(app) as client:
        r = client.post(f"/api/rooms/{rid}/start", json={"pipeline": False})
        assert r.status_code == 400


def test_candidate_listing_and_reject(temp_db: None) -> None:
    """候选可被列出并拒绝。"""
    from app.db.models import HighlightCandidate
    from app.db.session import get_session
    from app.web.main import app

    now = datetime.now(UTC)
    with get_session() as db:
        cand = HighlightCandidate(
            session_id=1,
            peak_ts=now,
            start_ts=now,
            end_ts=now + timedelta(seconds=30),
            highlight_score=0.8,
            reason="测试候选",
        )
        db.add(cand)
        db.flush()
        cid = cand.id

    with TestClient(app) as client:
        rows = client.get("/api/candidates").json()
        assert any(c["id"] == cid for c in rows)

        r = client.post(f"/api/candidates/{cid}/reject")
        assert r.status_code == 200

        rejected = client.get("/api/candidates?status=rejected").json()
        assert any(c["id"] == cid for c in rejected)


def test_trends_endpoint(temp_db: None) -> None:
    """网感资料库接口返回概览结构;未启用时 enabled=False。"""
    from app.web.main import app

    with TestClient(app) as client:
        r = client.get("/api/trends")
        assert r.status_code == 200
        data = r.json()
        assert "enabled" in data
        assert "items" in data
        assert "keywords" in data


def test_danmaku_overview(temp_db: None) -> None:
    """弹幕接口返回热度概览结构(已接入采集模块)。"""
    from app.web.main import app

    with TestClient(app) as client:
        r = client.get("/api/danmaku")
        assert r.status_code == 200
        data = r.json()
        assert data["available"] is True
        assert "recent" in data
        assert "sessions" in data


def test_task_listing_returns_worker_stats(temp_db: None) -> None:
    """任务列表接口应返回可序列化的 Worker 统计，而不是调用属性。"""
    from app.web.main import app

    with TestClient(app) as client:
        response = client.get("/api/tasks?limit=40")

    assert response.status_code == 200
    payload = response.json()
    assert payload["tasks"] == []
    assert isinstance(payload["stats"], dict)
    assert payload["stats"]["worker"]["transcribing"] == 0


def test_dashboard_page_renders(temp_db: None) -> None:
    """根路径返回带分组导航和响应式工作台外壳的仪表盘 HTML。"""
    from app.web.main import app

    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "BiliLiveCut" in r.text
        assert '<body class="dashboard-shell">' in r.text
        assert '<nav class="tabs" aria-label="工作台导航">' in r.text
        assert r.text.count('class="tabs-group"') == 4
        assert '<main class="dashboard-main">' in r.text
        assert "高光模型" not in r.text
        assert 'data-tab="features"' in r.text
        assert "五项流水线自动化开关" in r.text
        assert 'id="feature-switches-list"' in r.text


def test_llm_connectivity_uses_unsaved_form_payload(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """模型连通测试应使用当前表单配置，无需先持久化。"""
    from app.analysis import llm as llm_mod
    from app.analysis import llm_providers as provs
    from app.web.main import app

    def fake_complete(
        provider: provs.LLMProvider,
        prompt: str,
        max_tokens: int,
        extra_body: dict | None = None,
    ) -> str:
        assert provider.api_key == "draft-secret"
        assert prompt == "只回复 pong"
        assert max_tokens == 64
        assert extra_body is None
        return "pong"

    monkeypatch.setattr(llm_mod, "_complete", fake_complete)

    with TestClient(app) as client:
        response = client.post(
            "/api/llm-providers/test",
            json={
                "providers": [
                    {
                        "name": "草稿模型",
                        "base_url": "https://example.invalid/v1",
                        "model": "draft-model",
                        "api_key": "draft-secret",
                        "enabled": True,
                        "priority": 1,
                    }
                ]
            },
        )

    assert response.status_code == 200
    assert response.json()["results"] == [
        {"id": response.json()["results"][0]["id"], "name": "草稿模型", "ok": True, "detail": "pong"}
    ]
    assert provs._read_raw() == []  # noqa: SLF001


def test_llm_connectivity_rejects_empty_response(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """服务返回空正文时，连通测试必须显示失败而不是假阳性。"""
    from app.analysis import llm as llm_mod
    from app.web.main import app

    monkeypatch.setattr(llm_mod, "_complete", lambda *_args, **_kwargs: "  ")

    with TestClient(app) as client:
        response = client.post(
            "/api/llm-providers/test",
            json={
                "providers": [
                    {
                        "name": "空响应模型",
                        "base_url": "https://example.invalid/v1",
                        "model": "empty-model",
                        "api_key": "draft-secret",
                        "enabled": True,
                    }
                ]
            },
        )

    assert response.status_code == 200
    assert response.json()["results"][0]["ok"] is False
    assert "未返回可用正文" in response.json()["results"][0]["detail"]


def test_dashboard_serves_complete_javascript_module_graph(temp_db: None) -> None:
    """主页引用的入口脚本及其所有直接 ES Module 依赖必须可访问。"""
    from app.web.main import app

    with TestClient(app) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert '<script type="module" src="/static/app.js"></script>' in dashboard.text

        entrypoint = client.get("/static/app.js")
        assert entrypoint.status_code == 200
        assert "javascript" in entrypoint.headers["content-type"]

        imports = re.findall(r'from\s+"(\./js/[^"]+\.js)"', entrypoint.text)
        assert imports
        for module_path in imports:
            response = client.get(f"/static/{module_path.removeprefix('./')}")
            assert response.status_code == 200, module_path
            assert response.content, module_path


def test_settings_toggle_and_uploads(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """上传开关默认关闭,可切换;上传队列与打开目录接口工作。"""
    from app.web import service
    from app.web.main import app

    # 避免测试真的打开文件管理器窗口。
    monkeypatch.setattr(service, "open_path", lambda p: True)

    with TestClient(app) as client:
        s = client.get("/api/settings").json()
        assert s["recording_pipeline_enabled"] is True
        assert s["recording_pipeline_overridden"] is False
        assert s["transcript_llm_refine_enabled"] is True
        assert s["transcript_llm_refine_overridden"] is False
        assert s["biliup_enabled"] is False
        assert s["upload_active"] is False

        s2 = client.patch(
            "/api/settings",
            json={
                "recording_pipeline_enabled": False,
                "transcript_llm_refine_enabled": False,
                "biliup_enabled": True,
            },
        ).json()
        assert s2["recording_pipeline_enabled"] is False
        assert s2["recording_pipeline_overridden"] is True
        assert s2["transcript_llm_refine_enabled"] is False
        assert s2["transcript_llm_refine_overridden"] is True
        assert s2["biliup_enabled"] is True
        assert s2["upload_active"] is True

        assert client.get("/api/uploads").json() == []
        assert client.get("/api/notifications").json() == []

        r = client.post("/api/open-clips-dir")
        assert r.status_code == 200
        assert "clips_dir" in r.json()


def test_transcript_api_exposes_summary_and_raw_asr(temp_db: None) -> None:
    """实时转写接口应区分 LLM 整理正文、片段摘要和原始 ASR。"""
    import json

    from app.db.models import Transcript
    from app.db.session import get_session
    from app.web.main import app

    with get_session() as db:
        db.add(
            Transcript(
                segment_id=9,
                language="zh",
                text="整理后的可读正文。",
                final_text="原始没有标点的转写",
                primary_backend="funasr-nano",
                auxiliary_json=json.dumps(
                    {"transcript_refinement": {"applied": True, "summary": "片段摘要"}},
                    ensure_ascii=False,
                ),
            )
        )

    with TestClient(app) as client:
        row = client.get("/api/transcripts?limit=1").json()[0]

    assert row["text"] == "整理后的可读正文。"
    assert row["raw_text"] == "原始没有标点的转写"
    assert row["summary"] == "片段摘要"
    assert row["llm_refined"] is True
    assert row["primary_backend"] == "funasr-nano"


def test_retranscribe_api_deletes_transcript_and_requeues_segment(temp_db: None) -> None:
    """无受保护下游资产时，重新识别应原子删除旧转写并重置任务。"""
    from app.db.models import RawSegment, SegmentStatus, SegmentTask, TaskStatus, Transcript
    from app.db.session import get_session
    from app.web.main import app

    with get_session() as db:
        segment = RawSegment(session_id=1, seq=1, file_path="segment.ts", status=SegmentStatus.SCORED)
        db.add(segment)
        db.flush()
        transcript = Transcript(segment_id=segment.id, text="被污染的旧转写")
        db.add(transcript)
        task = SegmentTask(
            segment_id=segment.id,
            session_id=segment.session_id,
            stage=TaskStatus.COMPLETED,
            pipeline_key=f"pipeline:{segment.id}",
        )
        db.add(task)
        db.flush()
        transcript_id = transcript.id
        task_id = task.id
        segment_id = segment.id

    with TestClient(app) as client:
        response = client.post(f"/api/transcripts/{transcript_id}/retranscribe")

    assert response.status_code == 200
    assert response.json() == {"task_id": task_id, "segment_id": segment_id}
    with get_session() as db:
        assert db.get(Transcript, transcript_id) is None
        segment = db.get(RawSegment, segment_id)
        task = db.get(SegmentTask, task_id)
        assert segment is not None and segment.status == SegmentStatus.RECORDED
        assert task is not None and task.stage == TaskStatus.QUEUED_FOR_TRANS
        assert task.candidate_id is None
        assert task.event_id is None
        assert task.completed_at is None


def test_retranscribe_api_cleans_only_automatic_unrendered_analysis(temp_db: None) -> None:
    """未人工处理且未渲染的候选、事件和自动主题关联可随污染转写重建。"""
    from datetime import UTC, datetime, timedelta

    from app.db.models import (
        HighlightCandidate,
        HighlightEvent,
        HighlightTopic,
        RawSegment,
        SegmentTask,
        TaskStatus,
        Topic,
        Transcript,
    )
    from app.db.session import get_session
    from app.web.main import app

    now = datetime.now(UTC)
    with get_session() as db:
        segment = RawSegment(session_id=2, seq=1, file_path="segment.ts")
        db.add(segment)
        db.flush()
        transcript = Transcript(segment_id=segment.id, text="等一下" * 100)
        candidate = HighlightCandidate(
            session_id=2,
            peak_ts=now,
            start_ts=now,
            end_ts=now + timedelta(seconds=30),
            dedup_hash="auto-retranscribe-test",
        )
        topic = Topic(session_id=2, title="自动主题")
        db.add(candidate)
        db.add(topic)
        db.flush()
        event = HighlightEvent(candidate_id=candidate.id, session_id=2, topic_id=topic.id, review_by="auto")
        db.add(event)
        db.flush()
        membership = HighlightTopic(event_id=event.id, topic_id=topic.id, confidence=0.8)
        task = SegmentTask(
            segment_id=segment.id,
            session_id=2,
            stage=TaskStatus.AWAITING_REVIEW,
            candidate_id=candidate.id,
            event_id=event.id,
            pipeline_key=f"pipeline:{segment.id}",
        )
        db.add(transcript)
        db.add(membership)
        db.add(task)
        db.flush()
        transcript_id = transcript.id
        candidate_id = candidate.id
        event_id = event.id
        membership_id = membership.id

    with TestClient(app) as client:
        response = client.post(f"/api/transcripts/{transcript_id}/retranscribe")

    assert response.status_code == 200
    with get_session() as db:
        assert db.get(Transcript, transcript_id) is None
        assert db.get(HighlightCandidate, candidate_id) is None
        assert db.get(HighlightEvent, event_id) is None
        assert db.get(HighlightTopic, membership_id) is None


def test_retranscribe_api_preserves_manually_reviewed_assets(temp_db: None) -> None:
    """存在人工审核时接口应返回冲突，且不修改任何数据。"""
    from datetime import UTC, datetime, timedelta

    from app.db.models import HighlightCandidate, HighlightEvent, RawSegment, SegmentTask, TaskStatus, Transcript
    from app.db.session import get_session
    from app.web.main import app

    now = datetime.now(UTC)
    with get_session() as db:
        segment = RawSegment(session_id=3, seq=1, file_path="segment.ts")
        db.add(segment)
        db.flush()
        transcript = Transcript(segment_id=segment.id, text="旧转写")
        candidate = HighlightCandidate(
            session_id=3,
            peak_ts=now,
            start_ts=now,
            end_ts=now + timedelta(seconds=30),
            dedup_hash="manual-retranscribe-test",
        )
        db.add(candidate)
        db.flush()
        event = HighlightEvent(candidate_id=candidate.id, session_id=3, review_by="operator")
        db.add(event)
        db.flush()
        task = SegmentTask(
            segment_id=segment.id,
            session_id=3,
            stage=TaskStatus.AWAITING_REVIEW,
            candidate_id=candidate.id,
            event_id=event.id,
            pipeline_key=f"pipeline:{segment.id}",
        )
        db.add(transcript)
        db.add(task)
        db.flush()
        transcript_id = transcript.id
        candidate_id = candidate.id
        event_id = event.id

    with TestClient(app) as client:
        response = client.post(f"/api/transcripts/{transcript_id}/retranscribe")

    assert response.status_code == 409
    assert "人工审核" in response.json()["detail"]
    with get_session() as db:
        assert db.get(Transcript, transcript_id) is not None
        assert db.get(HighlightCandidate, candidate_id) is not None
        assert db.get(HighlightEvent, event_id) is not None
