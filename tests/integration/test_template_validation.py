from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db.entities import IntroTemplate, SubtitleTemplate
from app.db.session import get_session


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("subtitle", {"font_size": True}),
        ("subtitle", {"font_size": "36"}),
        ("subtitle", {"font_size": None}),
        ("subtitle", {"bold": True}),
        ("subtitle", {"bold": 2}),
        ("subtitle", {"alignment": 10}),
        ("subtitle", {"outline": -1}),
        ("subtitle", {"primary_color": "red;injected"}),
        ("subtitle", {"is_default": "false"}),
        ("subtitle", []),
        ("intro", {"intro_enabled": "false"}),
        ("intro", {"intro_font_size": -1}),
        ("intro", {"intro_duration_s": True}),
        ("intro", {"intro_font_color": "white:fontfile=untrusted"}),
        ("intro", {"intro_bg_color": "black@1.1"}),
        ("intro", {"intro_bg_color": "black@nan"}),
        ("intro", {"intro_text": None}),
    ],
)
def test_invalid_template_update_leaves_row_unchanged(temp_db: None, kind: str, payload: object) -> None:
    from app.web.main import app

    entity = SubtitleTemplate(name="original") if kind == "subtitle" else IntroTemplate(name="original")
    with get_session() as db:
        db.add(entity)
        db.flush()
        identity = entity.id
    path = "/api/templates" if kind == "subtitle" else "/api/intro-templates"
    with TestClient(app) as client:
        response = client.put(f"{path}/{identity}", json=payload)
        assert response.status_code == 422, response.text
    with get_session() as db:
        stored = db.get(type(entity), identity)
        assert stored is not None and stored.name == "original" and not stored.is_default


def test_partial_subtitle_update_validates_combined_display_times(temp_db: None) -> None:
    from app.web.main import app

    with get_session() as db:
        row = SubtitleTemplate(name="style", min_display_ms=1000, max_display_ms=3000)
        db.add(row)
        db.flush()
        identity = row.id
    with TestClient(app) as client:
        assert client.put(f"/api/templates/{identity}", json={"min_display_ms": 4000}).status_code == 422
        assert client.put(f"/api/templates/{identity}", json={"max_display_ms": 500}).status_code == 422
        accepted = client.put(
            f"/api/templates/{identity}", json={"min_display_ms": 4000, "max_display_ms": 5000, "description": None}
        )
        assert accepted.status_code == 200, accepted.text


def test_default_intro_values_and_multiline_variables_remain_supported(temp_db: None) -> None:
    from app.web.main import app

    with get_session() as db:
        row = IntroTemplate(name="intro")
        db.add(row)
        db.flush()
        identity = row.id
    with TestClient(app) as client:
        response = client.put(
            f"/api/intro-templates/{identity}",
            json={"intro_font_color": "white", "intro_bg_color": "black@0.6", "intro_text": "{streamer_name}\n你好"},
        )
        assert response.status_code == 200, response.text


def test_invalid_later_ass_style_does_not_commit_earlier_styles(temp_db: None) -> None:
    from app.web.main import app

    style = (
        b"Style: Uploaded,Arial,36,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,2,2,20,20,20,1\n"
    )
    content = b"[V4+ Styles]\n" + style + style.replace(b",36,", b",invalid,", 1)
    with get_session() as db:
        before = len(db.exec(select(SubtitleTemplate)).all())
    with TestClient(app) as client:
        response = client.post("/api/templates/import/ass", files={"file": ("style.ass", content, "text/plain")})
        assert response.status_code == 422, response.text
    with get_session() as db:
        assert len(db.exec(select(SubtitleTemplate)).all()) == before


def test_negative_ass_spacing_survives_import_update_and_export(temp_db: None) -> None:
    from app.web.main import app

    content = (
        b"[V4+ Styles]\n"
        b"Style: Tight,Arial,36,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        b"0,0,0,0,100,100,-1,0,1,2,2,2,20,20,20,1\n"
    )
    with TestClient(app) as client:
        imported = client.post("/api/templates/import/ass", files={"file": ("tight.ass", content, "text/plain")})
        assert imported.status_code == 200, imported.text
        identity = imported.json()["imported"][0]["id"]
        assert client.get(f"/api/templates/{identity}").json()["spacing"] == -1
        assert client.put(f"/api/templates/{identity}", json={"spacing": -2}).status_code == 200
        exported = client.get(f"/api/templates/{identity}/export")
        assert exported.status_code == 200 and ",100,100,-2," in exported.text


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_template_numbers_are_rejected_without_server_error(temp_db: None, literal: str) -> None:
    from app.web.main import app

    with get_session() as db:
        template = SubtitleTemplate(name="finite")
        db.add(template)
        db.flush()
        identity = template.id
    with TestClient(app) as client:
        response = client.put(
            f"/api/templates/{identity}",
            content='{"angle":' + literal + "}",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422, response.text
