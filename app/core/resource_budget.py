"""全局资源预算管理器。

在多任务流水线场景中,通过统一入口协调 CPU / GPU / 内存 / 显存的分配与释放,
避免资源争抢导致 OOM 或性能退化。

模块级单例及便捷函数可直接通过
``from app.core.resource_budget import acquire_resources, release_resources`` 使用。
"""

from __future__ import annotations

import threading

# ── 全局资源上限 ────────────────────────────────────────────────────────────
GLOBAL_CPU_TASKS_MAX = 2
GLOBAL_GPU_TASKS_MAX = 1
GLOBAL_MEMORY_BUDGET_MB = 4096
GLOBAL_VRAM_BUDGET_MB = 2048

# ── 任务阶段资源预估 ────────────────────────────────────────────────────────
# 各阶段在 CPU / GPU 模式下的资源消耗估算。
_TASK_COST: dict[str, dict[str, dict[str, int | float]]] = {
    "asr": {
        "cpu": {
            "cpu": 1,
            "gpu": 0,
            "memory_mb": 1500,
            "vram_mb": 0,
        },
        "gpu": {
            "cpu": 0,
            "gpu": 1,
            "memory_mb": 500,
            "vram_mb": 2000,
        },
    },
    "analysis": {
        "cpu": {
            "cpu": 0,
            "gpu": 0,
            "memory_mb": 200,
            "vram_mb": 0,
        },
    },
    "render": {
        "cpu": {
            "cpu": 1,
            "gpu": 0,
            "memory_mb": 500,
            "vram_mb": 0,
        },
    },
    "publish": {
        "cpu": {
            "cpu": 0,
            "gpu": 0,
            "memory_mb": 100,
            "vram_mb": 0,
        },
    },
}


def get_task_cost(stage: str, device: str = "cpu") -> dict[str, int | float]:
    """返回指定阶段在给定设备模式下的资源预估。

    :param stage: 任务阶段,可选值: ``"asr"`` / ``"analysis"`` / ``"render"`` / ``"publish"``。
    :param device: 设备模式,``"cpu"`` 或 ``"gpu"``。
    :returns: 包含 ``cpu`` / ``gpu`` / ``memory_mb`` / ``vram_mb`` 的资源预估字典。
    :raises ValueError: 阶段或设备参数无效时抛出。
    """
    stage_costs = _TASK_COST.get(stage)
    if stage_costs is None:
        raise ValueError(f"未知任务阶段: {stage},有效值: {list(_TASK_COST)}")
    device_costs = stage_costs.get(device)
    if device_costs is None:
        raise ValueError(f"任务阶段 {stage!r} 不支持设备模式 {device!r},有效值: {list(stage_costs)}")
    return dict(device_costs)


class ResourceBudget:
    """全局资源预算追踪器。

    以线程安全的方式管理 CPU / GPU / 内存 / 显存的并发占用,
    所有操作受模块级上限常量约束。
    """

    def __init__(self) -> None:
        """初始化资源计数器及线程锁。"""
        self._cpu_tasks: int = 0
        self._gpu_tasks: int = 0
        self._memory_mb: float = 0.0
        self._vram_mb: float = 0.0
        self._lock = threading.Lock()

    # ── 属性访问 ────────────────────────────────────────────────────────

    @property
    def cpu_tasks(self) -> int:
        """当前已分配的 CPU 任务数。"""
        return self._cpu_tasks

    @property
    def gpu_tasks(self) -> int:
        """当前已分配的 GPU 任务数。"""
        return self._gpu_tasks

    @property
    def memory_mb(self) -> float:
        """当前已分配的内存估算(MB)。"""
        return self._memory_mb

    @property
    def vram_mb(self) -> float:
        """当前已分配的显存估算(MB)。"""
        return self._vram_mb

    # ── 核心操作 ────────────────────────────────────────────────────────

    def acquire(
        self,
        cpu: int = 0,
        gpu: int = 0,
        memory_mb: float = 0.0,
        vram_mb: float = 0.0,
    ) -> bool:
        """尝试预留资源。若资源不足则不做任何修改。

        :param cpu: 需要的 CPU 任务槽位数。
        :param gpu: 需要的 GPU 任务槽位数。
        :param memory_mb: 需要的内存(MB)。
        :param vram_mb: 需要的显存(MB)。
        :returns: 资源充足并预留成功返回 ``True``,否则返回 ``False``。
        """
        with self._lock:
            new_cpu = self._cpu_tasks + cpu
            new_gpu = self._gpu_tasks + gpu
            new_mem = self._memory_mb + memory_mb
            new_vram = self._vram_mb + vram_mb

            if (
                new_cpu > GLOBAL_CPU_TASKS_MAX
                or new_gpu > GLOBAL_GPU_TASKS_MAX
                or new_mem > GLOBAL_MEMORY_BUDGET_MB
                or new_vram > GLOBAL_VRAM_BUDGET_MB
            ):
                return False

            self._cpu_tasks = new_cpu
            self._gpu_tasks = new_gpu
            self._memory_mb = new_mem
            self._vram_mb = new_vram
            return True

    def release(
        self,
        cpu: int = 0,
        gpu: int = 0,
        memory_mb: float = 0.0,
        vram_mb: float = 0.0,
    ) -> None:
        """释放之前预留的资源。

        :param cpu: 释放的 CPU 任务槽位数。
        :param gpu: 释放的 GPU 任务槽位数。
        :param memory_mb: 释放的内存(MB)。
        :param vram_mb: 释放的显存(MB)。
        """
        with self._lock:
            self._cpu_tasks = max(0, self._cpu_tasks - cpu)
            self._gpu_tasks = max(0, self._gpu_tasks - gpu)
            self._memory_mb = max(0.0, self._memory_mb - memory_mb)
            self._vram_mb = max(0.0, self._vram_mb - vram_mb)

    def available(self) -> dict[str, int | float]:
        """返回当前可用的各类资源余量。

        :returns: 包含 ``cpu`` / ``gpu`` / ``memory_mb`` / ``vram_mb`` 可用余量的字典。
        """
        with self._lock:
            return {
                "cpu": GLOBAL_CPU_TASKS_MAX - self._cpu_tasks,
                "gpu": GLOBAL_GPU_TASKS_MAX - self._gpu_tasks,
                "memory_mb": GLOBAL_MEMORY_BUDGET_MB - self._memory_mb,
                "vram_mb": GLOBAL_VRAM_BUDGET_MB - self._vram_mb,
            }


# ── 模块级单例与便捷函数 ────────────────────────────────────────────────────
_budget = ResourceBudget()


def acquire_resources(
    cpu: int = 0,
    gpu: int = 0,
    memory_mb: float = 0.0,
    vram_mb: float = 0.0,
) -> bool:
    """便捷函数:尝试预留资源。

    :param cpu: 需要的 CPU 任务槽位数。
    :param gpu: 需要的 GPU 任务槽位数。
    :param memory_mb: 需要的内存(MB)。
    :param vram_mb: 需要的显存(MB)。
    :returns: 资源充足并预留成功返回 ``True``,否则返回 ``False``。
    """
    return _budget.acquire(cpu=cpu, gpu=gpu, memory_mb=memory_mb, vram_mb=vram_mb)


def release_resources(
    cpu: int = 0,
    gpu: int = 0,
    memory_mb: float = 0.0,
    vram_mb: float = 0.0,
) -> None:
    """便捷函数:释放之前预留的资源。

    :param cpu: 释放的 CPU 任务槽位数。
    :param gpu: 释放的 GPU 任务槽位数。
    :param memory_mb: 释放的内存(MB)。
    :param vram_mb: 释放的显存(MB)。
    """
    _budget.release(cpu=cpu, gpu=gpu, memory_mb=memory_mb, vram_mb=vram_mb)
