"""可终止且复用模型缓存的有限 ASR 进程槽，数据库及 LLM 均留在父进程。"""

from __future__ import annotations

import atexit
import importlib
import json
import multiprocessing
import os
import queue
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from loguru import logger

from app.analysis.transcription.process_tree import ProcessTree

if TYPE_CHECKING:
    from loguru import Message

    from app.core.config import Settings


class ASRTimeoutError(TimeoutError):
    """音频时长相关的有限推理预算耗尽，子进程已经终止。"""


class ASRShutdownError(RuntimeError):
    """Worker 停止后不再执行新推理，已运行推理在宽限截止后退出。"""


class RemoteASRError(RuntimeError):
    """保留子进程根异常类型、原始异常链和已脱敏堆栈。"""


def _send(connection: Connection, payload: dict[str, Any]) -> None:
    """IPC 仅传 JSON，不反序列化来自 SDK 的 pickle 或 stdout。"""
    connection.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _receive(connection: Connection) -> dict[str, Any]:
    value = json.loads(connection.recv_bytes(maxlength=64 * 1024 * 1024))
    if not isinstance(value, dict):
        raise RuntimeError("ASR IPC message must be an object")
    return value


def _child_entry(connection: Connection, target: Callable[[Connection], None], parent_pid: int) -> None:
    """确认独立进程组建立后才允许父进程提交 SDK 请求。"""
    if os.name != "nt":
        os.setsid()

        def watch_parent() -> None:
            while os.getppid() == parent_pid:
                time.sleep(0.5)
            os.killpg(os.getpid(), signal.SIGKILL)

        threading.Thread(target=watch_parent, name="asr-parent-watch", daemon=True).start()
        if os.getppid() != parent_pid:
            os.killpg(os.getpid(), signal.SIGKILL)
    _send(connection, {"kind": "ready"})
    target(connection)


def _model_state(models: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{key: value for key, value in model.items() if key != "last_used_at"} for model in models]


def _request_emitter(connection: Connection, request_id: str) -> Callable[..., None]:
    send_lock = threading.Lock()

    def emit(kind: str, **payload: Any) -> None:
        with send_lock:
            _send(connection, {"kind": kind, "request_id": request_id, **payload})

    return emit


def _asr_child(connection: Connection) -> None:
    """每个槽仅执行一个纯 ASR 请求；模块入口可供 Windows spawn 导入。"""
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    # spawn 可能先导入 CLI 的数据库模块；显式丢弃子进程继承的配置缓存和引擎。
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.logging import _safe_record

    logger.remove()
    logger.configure(patcher=_safe_record)
    if (db_module := sys.modules.get("app.db.session")) is not None:
        db_module.engine.dispose()
        importlib.reload(db_module)
    from app.analysis import asr_metrics
    from app.analysis.model_pool import model_pool, preload_models
    from app.analysis.transcription.pipeline import get_default_pipeline
    from app.analysis.transcription.progress import set_progress_sink
    from app.core.config import Settings
    from app.core.runtime_settings import publish_settings, settings_scope
    from app.core.sanitize import sanitize_diagnostic

    emit: Callable[..., None] | None = None
    last_models: list[dict[str, object]] = []
    while True:
        if emit is not None and not connection.poll(1):
            model_pool.cleanup_idle()
            models = model_pool.infos()
            if _model_state(models) != _model_state(last_models):
                emit("idle_models", models=models)
                last_models = models
            continue
        try:
            request = _receive(connection)
        except EOFError:
            return
        if request.get("operation") == "shutdown":
            model_pool.cleanup_idle(force=True)
            return
        emit = _request_emitter(connection, request["request_id"])

        def progress(
            stage: str,
            details: dict[str, object],
            emit: Callable[..., None] = emit,
            request_id: str = request["request_id"],
        ) -> None:
            if stage == "remote_request":
                emit("authorization_required")
                permit = _receive(connection)
                if permit != {"kind": "authorization_granted", "request_id": request_id}:
                    raise RuntimeError("ASR remote request authorization rejected")
            emit("progress", stage=stage, details=details, models=model_pool.infos())

        set_progress_sink(progress)
        asr_metrics.set_event_sink(lambda event, values, emit=emit: emit("metric", event=event, values=values))

        def diagnostic(message: Message, emit: Callable[..., None] = emit) -> None:
            record = message.record
            emit(
                "diagnostic",
                level=record["level"].name,
                message=record["message"],
                origin=f"{record['name']}:{record['function']}:{record['line']}",
            )

        sink_id = logger.add(diagnostic, level="WARNING", diagnose=False, backtrace=False)
        try:
            values = Settings.model_validate(request["settings"]).model_dump()
            publish_settings(values, {})
            with settings_scope(fresh=True, refresh=False), logger.contextualize(**request["context"]):
                model_pool.cleanup_idle()
                if request.get("operation") == "preload":
                    preload_models(refresh=False)
                    result = None
                else:
                    progress("audio_validated", request["audio_evidence"])
                    result = get_default_pipeline()._transcribe_normalized(
                        request["audio_path"], request.get("initial_prompt")
                    )
                    result.metadata.setdefault("audio_evidence", request["audio_evidence"])
                model_pool.cleanup_idle()
                last_models = model_pool.infos()
                emit(
                    "result", result=asdict(result) if result is not None else None, models=last_models, pid=os.getpid()
                )
        except Exception as exc:  # 单请求边界：异常链返回父进程，不能伪装为空识别。
            emit(
                "error",
                error=sanitize_diagnostic(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}", limit=16000),
                models=model_pool.infos(),
            )
        finally:
            logger.remove(sink_id)
            set_progress_sink(None)
            asr_metrics.set_event_sink(None)


@dataclass
class _Slot:
    process: BaseProcess
    connection: Connection
    tree: ProcessTree
    busy: bool = True
    ready: bool = False
    last_used: float = field(default_factory=time.monotonic)
    models: list[dict[str, object]] = field(default_factory=list)
    context: dict[str, object] = field(default_factory=dict)
    stage: str = "starting"
    started: float = field(default_factory=time.monotonic)
    stage_started: float = field(default_factory=time.monotonic)
    settings: dict[str, object] = field(default_factory=dict)
    outgoing: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    incoming: queue.Queue[dict[str, Any] | Exception] = field(default_factory=queue.Queue)
    io_stop: threading.Event = field(default_factory=threading.Event)
    io_thread: threading.Thread | None = None
    reserved_roles: tuple[str, ...] = ()
    quarantined: bool = False


def _slot_io(slot: _Slot) -> None:
    """唯一 IPC 线程允许监管者在半帧读取或管道背压时终止真实进程。"""
    try:
        while not slot.io_stop.is_set():
            try:
                request = slot.outgoing.get_nowait()
            except queue.Empty:
                pass
            else:
                _send(slot.connection, request)
            if slot.connection.poll(0.05):
                slot.incoming.put(_receive(slot.connection))
    except (OSError, EOFError, ValueError, RuntimeError) as exc:
        slot.incoming.put(exc)


def _next_message(slot: _Slot, timeout: float = 0.2) -> dict[str, Any] | None:
    try:
        message = slot.incoming.get(timeout=timeout)
    except queue.Empty:
        return None
    if isinstance(message, Exception):
        raise RuntimeError(f"ASR IPC failed pid={slot.process.pid}: {type(message).__name__}") from message
    return message


def _roles(config: Settings) -> tuple[str, ...]:
    roles = {"fallback"} if config.asr_primary == "whisper" else {"primary"}
    if config.asr_primary != "whisper":
        if config.asr_primary != "openai_compatible" and config.asr_sensevoice and config.asr_sensevoice_enabled:
            roles.add("auxiliary")
        if config.asr_funasr_review and config.asr_primary == "paraformer":
            roles.add("review")
        if config.asr_fallback_whisper:
            roles.add("fallback")
    return tuple(sorted(roles))


class ASRProcessPool:
    """有限持久进程槽；父进程一次预约所需角色以避免跨进程超卖和死锁。"""

    def __init__(self, worker_target: Callable[[Connection], None] = _asr_child) -> None:
        self._target = worker_target
        self._condition = threading.Condition()
        self._slots: list[_Slot] = []
        self._roles: dict[str, int] = {}
        self._closed = False
        self._shutdown_at: float | None = None
        self._operations: dict[str, dict[str, object]] = {}
        self._starting: dict[BaseProcess, Connection] = {}

    @contextmanager
    def operation(self, context: dict[str, object]) -> Iterator[None]:
        """将父进程解码及临时文件的生命周期也纳入关闭监管。"""
        operation_id = uuid4().hex
        with self._condition:
            if self._closed:
                raise ASRShutdownError("ASR pool is stopping")
            self._operations[operation_id] = dict(context)
        try:
            yield
        finally:
            with self._condition:
                self._operations.pop(operation_id)
                self._condition.notify_all()

    def check_shutdown(self) -> None:
        """现有解码或推理操作在统一宽限截止后协作退出。"""
        if self._shutdown_at is not None and time.monotonic() >= self._shutdown_at:
            raise ASRShutdownError("ASR worker shutdown deadline reached")

    def _kill(self, slot: _Slot) -> None:
        slot.io_stop.set()
        slot.tree.terminate()
        slot.process.join(5)
        if slot.process.is_alive():
            slot.process.kill()
            slot.process.join(5)
        if slot.process.is_alive():
            raise RuntimeError(f"ASR 子进程无法终止 pid={slot.process.pid}；资源名额保持占用")
        if slot.io_thread is not None and slot.io_thread.ident is not None:
            slot.io_thread.join(5)
            if slot.io_thread.is_alive():
                raise RuntimeError(f"ASR IPC 未退出 pid={slot.process.pid}；资源名额保持占用")
        slot.connection.close()
        if slot in self._slots:
            self._slots.remove(slot)
        if slot.quarantined:
            self._release_roles(slot)

    def _release_roles(self, slot: _Slot) -> None:
        for role in slot.reserved_roles:
            self._roles[role] -= 1
        slot.reserved_roles = ()
        self._condition.notify_all()

    def _reap_failed(self, slot: _Slot) -> None:
        with self._condition:
            if slot in self._slots and slot.quarantined:
                self._kill(slot)

    def _reap_starting(self, process: BaseProcess) -> None:
        with self._condition:
            connection = self._starting.get(process)
            if connection is None:
                return
            if process.is_alive():
                process.kill()
            process.join(5)
            if process.is_alive():
                raise RuntimeError(f"ASR 启动失败后的进程尚未退出 pid={process.pid}")
            connection.close()
            self._starting.pop(process)
            self._condition.notify_all()

    def _retire(self, slot: _Slot) -> None:
        """空闲槽先正常退出，让模型库的 multiprocessing finalizer 释放信号量。"""
        if slot.ready and slot.process.is_alive():
            slot.outgoing.put({"operation": "shutdown"})
            slot.process.join(3)
        # SDK 卸载卡住或仍有后代时，仍由原有进程树监管兜底。
        self._kill(slot)

    def _check(self, deadline: float, checker: Callable[[], None], stage: str) -> None:
        checker()
        now = time.monotonic()
        if self._shutdown_at is not None and now >= self._shutdown_at:
            raise ASRShutdownError(f"ASR shutdown deadline reached stage={stage}")
        if now >= deadline:
            raise ASRTimeoutError(f"ASR inference deadline reached stage={stage}")

    def execute(
        self,
        request: dict[str, Any],
        *,
        timeout_s: float,
        check_cancelled: Callable[[], None],
        authorize_remote: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """等待资源、运行并监管一次请求，退出确认完成后才归还槽与角色名额。"""
        from app.core.config import Settings
        from app.core.runtime_settings import process_settings

        config = Settings.model_validate(request["settings"])
        roles = _roles(config)
        deadline = time.monotonic() + timeout_s
        slot: _Slot | None = None
        request = {**request, "request_id": uuid4().hex}
        with self._condition:
            while True:
                self._check(deadline, check_cancelled, "resource_wait")
                if self._closed:
                    raise ASRShutdownError("ASR pool is stopping")
                latest = process_settings()
                active = sum(item.busy for item in self._slots) + len(self._starting)
                task_id = request["context"].get("task_id")
                same_task_active = task_id is not None and any(
                    item.busy and item.context.get("task_id") == task_id for item in self._slots
                )
                if (
                    not same_task_active
                    and active < latest.asr_task_max_concurrency
                    and all(self._roles.get(role, 0) < getattr(latest, f"asr_{role}_max_concurrency") for role in roles)
                ):
                    break
                self._condition.wait(0.2)
            for item in list(self._slots):
                if not item.busy and not item.process.is_alive():
                    self._kill(item)
            slot = next((item for item in self._slots if not item.busy), None)
            if slot is None:
                parent, child = multiprocessing.get_context("spawn").Pipe()
                process = multiprocessing.get_context("spawn").Process(
                    target=_child_entry, args=(child, self._target, os.getpid()), name="BiliLiveCut-ASR"
                )
                try:
                    process.start()
                except BaseException:
                    parent.close()
                    child.close()
                    raise
                child.close()
                from app.core.execution import retain_cleanup

                self._starting[process] = parent
                release_starting = retain_cleanup(lambda: self._reap_starting(process))
                try:
                    tree = ProcessTree(process.pid)
                except BaseException:
                    self._reap_starting(process)
                    release_starting()
                    raise
                slot = _Slot(process, parent, tree)
                slot.io_thread = threading.Thread(
                    target=_slot_io, args=(slot,), name=f"asr-ipc-{process.pid}", daemon=True
                )
                self._slots.append(slot)
                self._starting.pop(process)
                release_starting()
                try:
                    slot.io_thread.start()
                except RuntimeError:
                    slot.quarantined = True
                    release_failed = retain_cleanup(lambda: self._reap_failed(slot))
                    self._kill(slot)
                    release_failed()
                    raise
            slot.busy = True
            slot.started = time.monotonic()
            slot.stage_started = slot.started
            slot.stage = "starting"
            slot.context = dict(request["context"])
            slot.settings = {
                key: value for key, value in config.model_dump().items() if key.startswith(("asr_", "whisper_"))
            }
            for role in roles:
                self._roles[role] = self._roles.get(role, 0) + 1
            slot.reserved_roles = roles
        finished = False
        try:
            logger.info("asr_process_start context={} pid={} budget_s={}", slot.context, slot.process.pid, timeout_s)
            while not slot.ready:
                self._check(deadline, check_cancelled, "starting")
                if (message := _next_message(slot)) is not None:
                    if message.get("kind") != "ready":
                        raise RuntimeError("ASR child readiness handshake failed")
                    slot.ready = True
                elif not slot.process.is_alive():
                    raise RuntimeError(f"ASR child startup failed exitcode={slot.process.exitcode}")
            slot.outgoing.put(request)
            while True:
                self._check(deadline, check_cancelled, slot.stage)
                message = _next_message(slot)
                if message is None:
                    if not slot.process.is_alive():
                        raise RuntimeError(
                            f"ASR child exited pid={slot.process.pid} "
                            f"exitcode={slot.process.exitcode} stage={slot.stage}"
                        )
                    continue
                if message.get("kind") == "idle_models":
                    slot.models = message["models"]
                    continue
                if message.get("request_id") != request["request_id"]:
                    raise RuntimeError("ASR IPC stale request result rejected")
                if "models" in message:
                    slot.models = message["models"]
                if message["kind"] == "authorization_required":
                    if authorize_remote is None:
                        raise RuntimeError("远程 ASR 缺少父进程授权检查")
                    authorize_remote()
                    slot.outgoing.put({"kind": "authorization_granted", "request_id": request["request_id"]})
                elif message["kind"] == "metric":
                    from app.analysis.asr_metrics import apply_event

                    apply_event(message["event"], message["values"])
                elif message["kind"] == "diagnostic":
                    from app.core.sanitize import sanitize_diagnostic

                    level = message["level"] if message["level"] in {"WARNING", "ERROR", "CRITICAL"} else "WARNING"
                    logger.log(
                        level,
                        "asr_diagnostic context={} pid={} stage={} origin={} {}",
                        slot.context,
                        slot.process.pid,
                        slot.stage,
                        message["origin"],
                        sanitize_diagnostic(message["message"], limit=16000),
                    )
                elif message["kind"] == "progress":
                    stage_elapsed = time.monotonic() - slot.stage_started
                    slot.stage_started = time.monotonic()
                    slot.stage = str(message["stage"])
                    logger.info(
                        "asr_progress context={} pid={} elapsed_s={:.1f} previous_stage_s={:.2f} stage={} details={}",
                        slot.context,
                        slot.process.pid,
                        time.monotonic() - slot.started,
                        stage_elapsed,
                        slot.stage,
                        message.get("details"),
                    )
                elif message["kind"] in {"error", "result"}:
                    self._check(deadline, check_cancelled, slot.stage)
                    if message["kind"] == "error":
                        raise RemoteASRError(message["error"])
                    finished = True
                    return message
                else:
                    raise RuntimeError("ASR IPC unknown message kind")
        finally:
            with self._condition:
                if not finished:
                    from app.core.execution import retain_cleanup

                    slot.quarantined = True
                    release = retain_cleanup(lambda: self._reap_failed(slot))
                    self._kill(slot)
                    release()
                    logger.warning(
                        "asr_process_terminated context={} pid={} stage={} elapsed_s={:.1f}",
                        slot.context,
                        slot.process.pid,
                        slot.stage,
                        time.monotonic() - slot.started,
                    )
                else:
                    slot.busy = False
                    slot.last_used = time.monotonic()
                    for model in slot.models:
                        model["active_users"] = 0
                    self._release_roles(slot)
                self._condition.notify_all()

    def shutdown(self, grace_s: float) -> None:
        """拒绝新请求并给现有推理统一宽限；监管线程负责终止与确认退出。"""
        deadline = time.monotonic() + grace_s
        with self._condition:
            self._closed = True
            self._shutdown_at = deadline
            self._condition.notify_all()
            for process in tuple(self._starting):
                self._reap_starting(process)
            for slot in self._slots:
                if slot.busy:
                    logger.info(
                        "asr_shutdown_wait context={} pid={} age_s={:.1f} stage={} grace_s={}",
                        slot.context,
                        slot.process.pid,
                        time.monotonic() - slot.started,
                        slot.stage,
                        grace_s,
                    )
            while any(slot.busy for slot in self._slots) or self._operations:
                for slot in list(self._slots):
                    if slot.quarantined:
                        self._kill(slot)
                self._condition.wait(0.2)
                if time.monotonic() > deadline + 12:
                    raise RuntimeError("ASR 监管线程未在终止宽限内归还进程槽")
            for slot in list(self._slots):
                self._retire(slot)

    def cleanup_idle(self) -> None:
        """回收闲置过期槽，正在推理的进程不受影响。"""
        from app.core.runtime_settings import process_settings

        config = process_settings()
        latest_settings = {
            key: value for key, value in config.model_dump().items() if key.startswith(("asr_", "whisper_"))
        }
        with self._condition:
            for process in tuple(self._starting):
                self._reap_starting(process)
            for slot in list(self._slots):
                if slot.quarantined:
                    self._kill(slot)
                    continue
                if slot.busy:
                    continue
                while not slot.incoming.empty():
                    try:
                        message = _next_message(slot, timeout=0)
                    except RuntimeError:
                        self._kill(slot)
                        break
                    if message is None:
                        break
                    if message.get("kind") != "idle_models":
                        raise RuntimeError("空闲 ASR 槽存在未结算响应")
                    slot.models = message["models"]
                if slot not in self._slots:
                    continue
                expired = (
                    config.asr_model_idle_unload_seconds > 0
                    and time.monotonic() - slot.last_used >= config.asr_model_idle_unload_seconds
                )
                if (
                    slot.settings != latest_settings
                    or len(self._slots) > config.asr_task_max_concurrency
                    or (expired and not any(getattr(config, f"asr_{role}_keep_loaded") for role in _roles(config)))
                ):
                    self._retire(slot)

    def has_active_task(self, task_id: int, lease_token: str) -> bool:
        """核对原任务是否仍占用进程、IPC 或标准化操作，不能提前结算取消。"""
        with self._condition:
            contexts = [slot.context for slot in self._slots if slot.busy]
            contexts.extend(self._operations.values())
            return any(
                context.get("task_id") == task_id and context.get("lease_token") == lease_token for context in contexts
            )

    def infos(self) -> list[dict[str, object]]:
        """返回子进程实测模型状态及监管阶段，不在父进程加载模型。"""
        with self._condition:
            return [
                {**model, "pid": slot.process.pid, "stage": slot.stage, "task": slot.context}
                for slot in self._slots
                for model in slot.models
            ]


_pool: ASRProcessPool | None = None
_pool_lock = threading.Lock()


def get_process_pool() -> ASRProcessPool:
    """取得应用进程的唯一资源监管器。"""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ASRProcessPool()
            atexit.register(_pool.shutdown, 0)
        return _pool


def reset_process_pool() -> None:
    """重启 Worker 时仅替换已关闭且没有运行槽的监管器。"""
    global _pool
    with _pool_lock:
        if _pool is not None:
            with _pool._condition:
                if _pool._closed:
                    if _pool._slots or _pool._operations or _pool._starting:
                        raise RuntimeError("ASR 监管器仍有活动进程或解码操作，不能重新启动")
                    _pool = None
