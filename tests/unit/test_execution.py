from __future__ import annotations

import asyncio
from threading import Event

import psutil
import pytest

from app.core.execution import retain_cleanup, run_sync
from app.core.supervisor import Service, Supervisor


async def test_repeated_cancellation_waits_for_real_thread_and_returns_unstarted_result() -> None:
    entered, release = Event(), Event()
    returned: list[int] = []

    def operation() -> int:
        entered.set()
        assert release.wait(5)
        return 73

    task = asyncio.create_task(run_sync(operation, returned.append))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        assert returned == []
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    assert returned == [73]


async def test_cancelled_thread_preserves_business_failure_after_exit() -> None:
    entered, release = Event(), Event()

    def operation() -> None:
        entered.set()
        assert release.wait(5)
        raise ValueError("upstream rejected request")

    task = asyncio.create_task(run_sync(operation))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(ValueError, match="upstream rejected"):
            await asyncio.wait_for(task, 3)


@pytest.mark.parametrize("error", [PermissionError("OS resource temporarily locked"), psutil.AccessDenied(pid=12)])
async def test_cleanup_failure_retains_execution_until_resource_can_be_reclaimed(error: Exception) -> None:
    attempted, release, reclaimed = Event(), Event(), Event()

    def cleanup() -> None:
        attempted.set()
        if not release.is_set():
            raise error
        reclaimed.set()

    def operation() -> None:
        retain_cleanup(cleanup)
        raise RuntimeError("processing failed")

    task = asyncio.create_task(run_sync(operation))
    try:
        assert await asyncio.to_thread(attempted.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not reclaimed.is_set()
    finally:
        release.set()
        with pytest.raises(RuntimeError, match="processing failed"):
            await asyncio.wait_for(task, 3)
    assert reclaimed.is_set()


async def test_supervisor_cleans_partial_start_in_reverse_order_and_signals_first() -> None:
    actions: list[str] = []

    async def first_start() -> None:
        actions.append("start:first")

    async def second_start() -> None:
        actions.append("start:second")
        raise OSError("startup failed")

    async def first_stop() -> None:
        actions.append("stop:first")

    async def second_stop() -> None:
        actions.append("stop:second")

    supervisor = Supervisor(
        [Service("first", first_start, first_stop), Service("second", second_start, second_stop)],
        lambda: actions.append("signal"),
    )
    with pytest.raises(OSError, match="startup failed"):
        async with supervisor.running():
            pytest.fail("failed startup reached serving state")
    assert actions == ["start:first", "start:second", "signal", "stop:second", "stop:first"]


async def test_supervisor_rejects_restart_after_unconfirmed_cleanup() -> None:
    async def failed_stop() -> None:
        raise RuntimeError("exit not confirmed")

    supervisor = Supervisor([Service("held", stop=failed_stop)], lambda: None)
    with pytest.raises(RuntimeError, match="exit not confirmed"):
        async with supervisor.running():
            with pytest.raises(RuntimeError, match="不能重复启动"):
                async with supervisor.running():
                    pytest.fail("duplicate startup")
    with pytest.raises(RuntimeError, match="关闭未完成"):
        async with supervisor.running():
            pytest.fail("restart after incomplete cleanup")


def test_supervisor_rejects_duplicate_service_identity() -> None:
    with pytest.raises(ValueError, match="名称必须唯一"):
        Supervisor([Service("worker"), Service("worker")], lambda: None)


async def test_supervisor_broadcasts_before_waiting_and_retains_overdue_cleanup() -> None:
    signals: list[str] = []
    stopped: list[str] = []
    release = Event()

    async def slow_stop() -> None:
        assert signals == ["global", "first", "slow"]
        await run_sync(lambda: release.wait(5))

    async def first_stop() -> None:
        stopped.append("first")

    supervisor = Supervisor(
        [
            Service("first", stop=first_stop, request_stop=lambda: signals.append("first")),
            Service("slow", stop=slow_stop, request_stop=lambda: signals.append("slow")),
        ],
        lambda: signals.append("global"),
        shutdown_timeout_s=0.05,
    )
    try:
        with pytest.raises(RuntimeError, match="尚未物理退出"):
            async with supervisor.running():
                pass
        assert stopped == ["first"]
        assert "slow" in supervisor._stopping and not supervisor._stopping["slow"].done()
        with pytest.raises(RuntimeError, match="关闭未完成"):
            async with supervisor.running():
                pytest.fail("unconfirmed cleanup cannot restart")
    finally:
        release.set()
        await asyncio.gather(*tuple(supervisor._stopping.values()))


async def test_notification_shutdown_cancellation_keeps_executor_until_real_delivery_finishes() -> None:
    from app.notify import delivery

    entered, release = Event(), Event()

    def deliver() -> bool:
        entered.set()
        assert release.wait(5)
        return True

    await delivery.start_email_delivery()
    assert delivery._enqueue("shutdown-probe", deliver)
    assert await asyncio.to_thread(entered.wait, 2)
    stopping = asyncio.create_task(delivery.stop_email_delivery())
    try:
        await asyncio.sleep(0)
        stopping.cancel()
        await asyncio.sleep(0)
        assert not stopping.done() and delivery._executor is not None
        assert not delivery._enqueue("new-probe", deliver)
        with pytest.raises(RuntimeError, match="尚未确认关闭"):
            await delivery.start_email_delivery()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopping, 3)
        await delivery.stop_email_delivery()
    assert delivery._executor is None


async def test_supervisor_broadcast_failure_still_notifies_and_closes_every_service() -> None:
    actions: list[str] = []

    def failed_signal() -> None:
        actions.append("failed-signal")
        raise OSError("control channel failed")

    async def stop() -> None:
        actions.append("stop")

    supervisor = Supervisor(
        [
            Service("first", stop=stop, request_stop=failed_signal),
            Service("second", stop=stop, request_stop=lambda: actions.append("second")),
        ],
        lambda: actions.append("global"),
    )
    with pytest.raises(ExceptionGroup, match="停止广播失败"):
        async with supervisor.running():
            pass
    assert actions == ["global", "failed-signal", "second", "stop", "stop"]
    assert not supervisor.shutdown_confirmed


async def test_shutdown_budget_is_frozen_at_stop_and_visible_to_preexisting_tasks() -> None:
    from app.core.supervisor import process_shutdown_control, remaining_shutdown, standalone_process

    ready, stopping = asyncio.Event(), asyncio.Event()
    timeout = 30.0
    observed: list[float] = []

    async def service_lifespan() -> None:
        async def stop() -> None:
            observed.append(remaining_shutdown(30))

        supervisor = Supervisor([Service("test", stop=stop)], lambda: None, shutdown_timeout_s=lambda: timeout)
        async with supervisor.running():
            ready.set()
            await stopping.wait()
        assert supervisor.shutdown_confirmed

    with standalone_process() as control:
        task = asyncio.create_task(service_lifespan())
        await ready.wait()
        timeout = 0.5
        control.begin(timeout)
        expires = control.expires_at
        control.begin(60)
        assert control.expires_at == expires
        await asyncio.sleep(0.02)
        stopping.set()
        await task
        assert process_shutdown_control() is control
        assert 0 < observed[0] < timeout
    assert process_shutdown_control() is None


def test_identity_polling_is_bounded_and_commit_boundary_forces_current_check() -> None:
    from app.core.cancellation import OperationCancelledError, cancellation_scope, check_cancellation

    calls = 0
    cancelled = False

    def check() -> bool:
        nonlocal calls
        calls += 1
        return cancelled

    with cancellation_scope(check, poll_interval_s=60):
        for _ in range(20):
            check_cancellation(force=False)
        assert calls == 1
        cancelled = True
        with pytest.raises(OperationCancelledError):
            check_cancellation()
        assert calls == 2


async def test_trend_shutdown_preserves_running_collection_and_blocks_restart(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis import llm
    from app.core.config import settings
    from app.core.configuration import ConfigurationChange, save_configuration
    from app.trends.scheduler import TrendScheduler
    from app.trends.store import recent_trends

    save_configuration(
        ConfigurationChange(
            values={
                "trend_enabled": True,
                "trend_schedule_enabled": True,
                "trend_schedule_start": "00:00",
                "trend_schedule_end": "00:00",
            }
        )
    )
    monkeypatch.setattr(settings, "worker_shutdown_timeout_seconds", 1)
    entered, release = Event(), Event()

    def model_response(*args: object, **kwargs: object) -> str:
        entered.set()
        assert release.wait(5)
        return '[{"title":"shutdown result","source":"web","heat":80}]'

    monkeypatch.setattr(llm, "call_trend_search", model_response)
    scheduler = TrendScheduler()
    scheduler.start()
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(RuntimeError, match="尚未物理退出"):
            await scheduler.stop()
        assert scheduler._collecting and scheduler._task is not None and not scheduler._task.done()
        with pytest.raises(RuntimeError, match="不能重新启动"):
            scheduler.start()
    finally:
        release.set()
        if scheduler._task is not None:
            await asyncio.wait_for(scheduler._task, 3)
        await scheduler.stop()
    assert not scheduler._collecting
    assert any(record.title == "shutdown result" for record in recent_trends())
