"""CLI commands behavioral tests — validates ALL_COMMANDS structure and Typer app wiring."""

from __future__ import annotations

import subprocess
import sys

import typer


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


def test_cli_module_entrypoint_dispatches_commands() -> None:
    """``python -m app.cli`` 必须执行 Typer，而不是静默退出。"""
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "version"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "BiliLiveCut 0.1.15.2-alpha" in result.stdout


def test_record_pipeline_persists_scheduler_switches(temp_db: None, monkeypatch) -> None:  # noqa: ANN001
    """CLI 显式流水线参数应同步房间开关并把 db_id 传给回调。"""
    from app.commands import record as record_cmd
    from app.db.models import LiveRoom
    from app.db.session import get_session

    with get_session() as db:
        room = LiveRoom(input_url="https://live.bilibili.com/1", room_id=1, authorized=True)
        db.add(room)
        db.flush()
        db_id = room.id

    callback_args: dict[str, object] = {}

    def fake_callback(**kwargs):  # noqa: ANN003, ANN202
        callback_args.update(kwargs)

        async def _callback(_segment) -> None:  # noqa: ANN001
            return None

        return _callback

    def fake_run(coro) -> None:  # noqa: ANN001
        coro.close()

    monkeypatch.setattr("app.pipeline.orchestrator.make_pipeline_callback", fake_callback)
    monkeypatch.setattr(record_cmd.asyncio, "run", fake_run)

    record_cmd.cmd_record(db_id, pipeline=True, produce=True)

    with get_session() as db:
        updated = db.get(LiveRoom, db_id)
        assert updated is not None
        assert updated.auto_analyze is True
        assert updated.auto_render is True
    assert callback_args == {"produce": True, "room_id": db_id}


def test_record_rejects_produce_without_pipeline() -> None:
    """避免接受永远不会生效的 ``--produce`` 组合。"""
    from app.commands.record import cmd_record

    try:
        cmd_record(1, pipeline=False, produce=True)
    except typer.Exit as exc:
        assert exc.exit_code == 1
    else:  # pragma: no cover - 明确表达必须抛错
        raise AssertionError("--produce without --pipeline must fail")
