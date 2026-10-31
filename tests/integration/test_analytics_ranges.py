from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event

from app.db import session as database
from app.db.entities import FinalClip, HighlightCandidate, LiveRoom, RecordingSession
from app.web.routers import analytics

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, ExecutionContext


NOW = datetime(2026, 1, 1, 0, 15, tzinfo=UTC)


class AnalyticsClock(datetime):
    @classmethod
    def now(cls, tz: object = None) -> datetime:
        return NOW


def seed_analytics() -> None:
    with database.get_session() as db:
        rooms = [
            LiveRoom(room_id=100 + index, input_url=str(100 + index), uploader_name=name)
            for index, name in enumerate(("主播", None, ""))
        ]
        db.add_all(rooms)
        db.flush()
        for index, offset in enumerate((0, 6, 7, 29, 30, 89, 90)):
            instant = NOW.replace(hour=0, minute=0) - timedelta(days=offset)
            recording = RecordingSession(
                room_id=rooms[index % 3].id,
                started_at=instant,
                ended_at=instant + timedelta(minutes=10) if index % 2 == 0 else None,
            )
            db.add(recording)
            db.flush()
            candidate = HighlightCandidate(
                session_id=recording.id,
                peak_ts=instant,
                start_ts=instant,
                end_ts=instant + timedelta(seconds=30),
                created_at=instant,
                dedup_hash=f"analytics-{index}",
                highlight_score=(0, 0.3, 0.5, 0.7, 0.85, 1, 0.9)[index],
            )
            db.add(candidate)
            db.flush()
            db.add(
                FinalClip(candidate_id=candidate.id, file_path=f"clips/{index}.mp4", duration_s=60, created_at=instant)
            )


def analytics_app() -> FastAPI:
    app = FastAPI()
    app.include_router(analytics.router, prefix="/api")
    return app


@pytest.mark.parametrize("days,expected", [(7, 2), (30, 4), (90, 6)])
async def test_selected_calendar_range_applies_to_statistics(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, days: int, expected: int
) -> None:
    monkeypatch.setattr(analytics, "datetime", AnalyticsClock, raising=False)
    seed_analytics()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=analytics_app()), base_url="http://test") as client:
        response = await client.get(f"/api/analytics?days={days}")
    assert response.status_code == 200
    result = response.json()
    daily = result["daily_trend"]
    assert len(daily) == days
    assert daily[0]["date"] == (NOW - timedelta(days=days - 1)).date().isoformat()
    assert daily[-1] == {"date": "2026-01-01", "sessions": 1, "clips": 1, "candidates": 1}
    assert sum(row["sessions"] for row in daily) == expected
    assert result["overview"]["total_sessions"] == expected
    assert result["overview"]["finished_sessions"] == expected // 2
    assert result["overview"]["total_clips"] == expected
    assert result["overview"]["total_candidates"] == expected
    assert sum(result["score_distribution"].values()) == expected
    assert sum(room["clips"] for room in result["room_ranking"]) == expected
    assert any(room["name"] == "房间101" for room in result["room_ranking"])
    if days >= 30:
        assert any(room["name"] == "房间102" for room in result["room_ranking"])


@pytest.mark.parametrize("value", ["0", "366", "-1", "invalid"])
async def test_analytics_rejects_invalid_range(temp_db: None, value: str) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=analytics_app()), base_url="http://test") as client:
        response = await client.get(f"/api/analytics?days={value}")
    assert response.status_code == 422


async def test_analytics_queries_do_not_grow_with_days_or_candidates(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analytics, "datetime", AnalyticsClock, raising=False)
    seed_analytics()
    statements: list[str] = []

    def observe(
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", observe)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=analytics_app()), base_url="http://test"
        ) as client:
            first = await client.get("/api/analytics?days=7")
            assert first.status_code == 200
            short_count = len(statements)
            statements.clear()
            with database.get_session() as db:
                db.add_all(
                    [
                        HighlightCandidate(
                            session_id=1,
                            peak_ts=NOW,
                            start_ts=NOW,
                            end_ts=NOW,
                            created_at=NOW,
                            dedup_hash=f"bulk-{i}",
                            highlight_score=0.85,
                        )
                        for i in range(1000)
                    ]
                )
            statements.clear()
            second = await client.get("/api/analytics?days=90")
            assert second.status_code == 200
            assert second.json()["score_distribution"]["0.85-1.0"] == 1002
            assert len(statements) == short_count
            assert not any("SELECT highlight_candidates.highlight_score " in sql for sql in statements)
    finally:
        event.remove(database.engine, "before_cursor_execute", observe)
