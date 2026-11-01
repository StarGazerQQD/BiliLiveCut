from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("password", ["change-me", " CHANGE-ME ", "", "   "])
def test_placeholder_reviewer_cannot_be_enabled(password: str) -> None:
    from app.web.main import _load_reviewer_passwords

    assert _load_reviewer_passwords(json.dumps({"reviewer": password})) == {}


def test_valid_reviewer_is_preserved_when_placeholder_is_rejected() -> None:
    from app.web.main import _load_reviewer_passwords

    accounts = {"reviewer": "change-me", "editor": "a-real-custom-password"}
    assert _load_reviewer_passwords(json.dumps(accounts)) == {"editor": "a-real-custom-password"}


@pytest.mark.parametrize("username", ["admin", "Admin", " editor", "editor\n"])
def test_invalid_reviewer_identity_is_rejected(username: str) -> None:
    from app.web.main import _load_reviewer_passwords

    assert _load_reviewer_passwords(json.dumps({username: "a-real-custom-password"})) == {}


def test_environment_template_has_no_default_reviewer() -> None:
    from dotenv import dotenv_values

    template = Path(__file__).resolve().parents[2] / ".env.example"
    assert json.loads(dotenv_values(template)["REVIEWER_ACCOUNTS_JSON"] or "{}") == {}
