"""Cookie module test — validates get_bilibili_cookie returns a string.

Imports the module to ensure it's counted in coverage.
"""

from __future__ import annotations


def test_get_cookie_returns_string() -> None:
    """get_bilibili_cookie returns an empty string or non-empty cookie."""
    from app.core.cookie import get_bilibili_cookie

    result = get_bilibili_cookie()
    assert isinstance(result, str)
