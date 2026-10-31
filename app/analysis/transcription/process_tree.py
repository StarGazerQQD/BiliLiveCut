"""受监管子进程树的所有权；父进程退出时不遗留原生推理或媒体处理。"""

from __future__ import annotations

import ctypes
import os
import signal
import time
from ctypes import wintypes
from threading import Lock

import psutil

from app.core.sanitize import safe_exception_summary
from app.core.supervisor import shutdown_deadline


class ProcessTree:
    """Windows 用 kill-on-close Job，POSIX 用子进程创建的独立进程组。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._lock = Lock()
        self._terminated = False
        self._signal_sent = False
        self._group_missing = False
        self._handle: int | None = None
        self._parent_pipe: int | None = None
        self.guardian_pid: int | None = None
        if os.name != "nt":
            return

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("ProcessTime", ctypes.c_longlong),
                ("JobTime", ctypes.c_longlong),
                ("Flags", wintypes.DWORD),
                ("MinWorkingSet", ctypes.c_size_t),
                ("MaxWorkingSet", ctypes.c_size_t),
                ("ActiveProcesses", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IOCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperations",
                    "WriteOperations",
                    "OtherOperations",
                    "ReadBytes",
                    "WriteBytes",
                    "OtherBytes",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("Basic", BasicLimits),
                ("IO", IOCounters),
                ("ProcessMemory", ctypes.c_size_t),
                ("JobMemory", ctypes.c_size_t),
                ("PeakProcessMemory", ctypes.c_size_t),
                ("PeakJobMemory", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel = kernel
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not kernel.AssignProcessToJobObject(handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                kernel.CloseHandle(process)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        self._handle = handle

    def own_parent_pipe(self, descriptor: int) -> None:
        """保留 POSIX 父死亡通知写端，随进程退出自动关闭。"""
        self._parent_pipe = descriptor

    def resume_suspended(self) -> None:
        """Windows 在 Job 接管完成后恢复新建的挂起进程；失败时回收独占进程树。"""
        if os.name != "nt":
            return
        try:
            psutil.Process(self.pid).resume()
        except BaseException as error:
            # 包括启动被中断的情况，均不能遗留挂起进程或开放的 Job 句柄。
            try:
                self.terminate()
            except (OSError, RuntimeError, psutil.Error) as cleanup_error:
                detail = safe_exception_summary(cleanup_error)
                error.add_note(f"恢复进程失败后的进程树清理失败 pid={self.pid}: {detail}")
            raise

    def terminate(self) -> None:
        """终止本监管器独占的进程树；调用方再用 join/wait 确认启动者退出。"""
        with self._lock:
            if self._terminated:
                return
            self._terminate()
            if self._parent_pipe is not None:
                os.close(self._parent_pipe)
                self._parent_pipe = None
            # 成功终止后不再发送信号：Darwin 的僵尸进程组可能返回 EPERM，
            # 回收后的 PID/PGID 也可能被其他进程复用。失败则保留所有权供重试。
            # 调用方仍须通过 join/wait/communicate 确认退出和管道收尾。
            self._terminated = True

    def _terminate(self) -> None:
        if os.name == "nt":
            if self._handle is not None:

                class Accounting(ctypes.Structure):
                    _fields_ = [
                        (name, ctypes.c_longlong)
                        for name in ("UserTime", "KernelTime", "PeriodUserTime", "PeriodKernelTime")
                    ] + [
                        (name, wintypes.DWORD)
                        for name in ("PageFaults", "TotalProcesses", "ActiveProcesses", "TerminatedProcesses")
                    ]

                if not self._kernel.TerminateJobObject(self._handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                deadline = min(time.monotonic() + 5, shutdown_deadline(5))
                while True:
                    accounting = Accounting()
                    if not self._kernel.QueryInformationJobObject(
                        self._handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
                    ):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if accounting.ActiveProcesses == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"进程树终止后仍有 {accounting.ActiveProcesses} 个活动进程 pid={self.pid}")
                    time.sleep(0.02)
                if not self._kernel.CloseHandle(self._handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._handle = None
        else:
            if not self._signal_sent:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    # spawn 尚未执行 setsid 时没有同 PID 进程组，直接终止启动者。
                    self._group_missing = True
                    try:
                        os.kill(self.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                self._signal_sent = True
            self._wait_for_posix_exit()

    def _wait_for_posix_exit(self) -> None:
        deadline = min(time.monotonic() + 5, shutdown_deadline(5))
        while True:
            active = False
            for process in psutil.process_iter(attrs=["pid", "status"]):
                try:
                    owned = process.pid == self.pid if self._group_missing else os.getpgid(process.pid) == self.pid
                except ProcessLookupError:
                    continue
                if owned and process.info["status"] not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                    active = True
                    break
                if owned and process.pid != self.pid:
                    try:
                        # 只回收已被 PID 1/subreaper 收养的本组子代；直接子进程由 Popen 保留退出码。
                        os.waitpid(process.pid, os.WNOHANG)
                    except ChildProcessError:
                        pass  # 其它 init 持有回收权。
            if not active:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"终止信号已发送，但进程组仍有活动成员 pgid={self.pid}")
            time.sleep(0.02)
