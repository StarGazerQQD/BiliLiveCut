from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "app" / "web" / "templates"
STYLE = ROOT / "app" / "web" / "static" / "style.css"
REVIEW_SCRIPT = ROOT / "app" / "web" / "static" / "js" / "review.js"


def _read_template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _seed_review_candidate() -> int:
    from app.db.models import HighlightCandidate, LiveRoom, RecordingSession, SessionStatus
    from app.db.session import get_session

    now = datetime.now(UTC).replace(microsecond=0)
    with get_session() as db:
        room = LiveRoom(input_url="ui-test", room_id=903, authorized=True)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id, status=SessionStatus.STOPPED, ended_at=now)
        db.add(session)
        db.flush()
        candidate = HighlightCandidate(
            session_id=session.id,
            peak_ts=now,
            start_ts=now - timedelta(seconds=15),
            end_ts=now + timedelta(seconds=15),
            highlight_score=0.8,
        )
        db.add(candidate)
        db.flush()
        candidate_id = candidate.id
    assert candidate_id is not None
    return candidate_id


def test_dashboard_navigation_matches_panels() -> None:
    html = _read_template("dashboard.html")
    tabs = re.findall(r'data-tab="([^"]+)"', html)
    panels = set(re.findall(r'id="tab-([^"]+)"', html))

    assert 'body class="dashboard-shell"' in html
    assert len(tabs) == len(set(tabs)) == 18
    assert set(tabs) == panels
    assert html.count('class="tabs-group"') == 4
    assert 'aria-label="工作台导航"' in html


def test_standalone_pages_share_design_system_without_inline_theme() -> None:
    expected_classes = {
        "review.html": "review-shell",
        "review_queue.html": "standalone-shell queue-shell",
        "collection.html": "standalone-shell collection-shell",
    }

    for name, body_class in expected_classes.items():
        html = _read_template(name)
        assert f'class="{body_class}"' in html
        assert "<style" not in html
        assert 'name="viewport"' in html
        assert 'href="/static/style.css?v=2"' in html


def test_design_system_covers_responsive_and_accessible_states() -> None:
    css = STYLE.read_text(encoding="utf-8")

    for token in ("--focus", "--text-soft", "--accent-soft", "--radius-lg"):
        assert token in css
    assert ":focus-visible" in css
    assert "@media (max-width: 820px)" in css
    assert "@media (max-width: 620px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_review_and_collection_keep_interaction_contracts() -> None:
    review = _read_template("review.html")
    collection = _read_template("collection.html")

    for element_id in (
        "player",
        "in-point",
        "out-point",
        "btn-claim",
        "review-reason",
        "render-job-status",
    ):
        assert f'id="{element_id}"' in review
    for element_id in (
        "event-list",
        "btn-render",
        "btn-cancel-render",
        "render-status",
        "copywriter-output",
    ):
        assert f'id="{element_id}"' in collection


def test_candidate_review_actions_use_closed_template_literals() -> None:
    script = REVIEW_SCRIPT.read_text(encoding="utf-8")

    assert 'api("POST", `/api/candidates/${id}/reject`)' in script
    assert 'api("POST", `/api/candidates/${id}/reject")' not in script


def test_standalone_ui_routes_render(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.web import main

    candidate_id = _seed_review_candidate()
    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "ui-test-pass")
    auth = ("admin", "ui-test-pass")
    with TestClient(main.app) as client:
        queue = client.get("/review/queue", auth=auth)
        review = client.get(f"/review/{candidate_id}", auth=auth)
        collection = client.get("/collection/1", auth=auth)

    assert queue.status_code == 200
    assert 'class="standalone-shell queue-shell"' in queue.text
    assert review.status_code == 200
    assert 'class="review-shell"' in review.text
    assert collection.status_code == 200
    assert 'class="standalone-shell collection-shell"' in collection.text
