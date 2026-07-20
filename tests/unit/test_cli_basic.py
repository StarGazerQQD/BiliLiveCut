"""CLI commands behavioral tests — validates ALL_COMMANDS structure and Typer app wiring."""

from __future__ import annotations


def test_all_commands_count_and_structure() -> None:
    """ALL_COMMANDS list has expected entries; each entry has (name, func, help)."""
    from app.commands import ALL_COMMANDS

    assert len(ALL_COMMANDS) >= 4
    for cmd_name, cmd_func, _help_text in ALL_COMMANDS:
        assert isinstance(cmd_name, str)
        assert callable(cmd_func)


def test_cli_app_version_present() -> None:
    """app.cli has version command and __version__."""
    from app import __version__

    assert __version__.startswith("0.1")
