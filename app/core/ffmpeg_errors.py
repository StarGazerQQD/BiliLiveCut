"""FFmpeg 错误分类模块。

提供结构化的 FFmpeg/FFprobe 错误类型枚举、分类函数和可重试性判断,
供切片流水线和任务队列使用,便于精确区分瞬时故障与永久失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class FfmpegErrorType(Enum):
    """FFmpeg 错误类型枚举。

    用于分类 FFmpeg 子进程失败的具体原因,
    支撑任务队列的重试决策与错误上报。
    """

    TRANSIENT_NETWORK = auto()
    """瞬时网络错误: Connection refused/reset、DNS 解析失败、超时等。"""

    UPSTREAM_UNAVAILABLE = auto()
    """上游服务不可用: HTTP 4xx/5xx 响应。"""

    DISK_FULL = auto()
    """磁盘空间不足。"""

    PERMISSION_DENIED = auto()
    """权限拒绝。"""

    INVALID_ARGUMENT = auto()
    """无效参数或未知选项。"""

    MISSING_BINARY = auto()
    """FFmpeg/FFprobe 二进制文件缺失。"""

    UNSUPPORTED_CODEC = auto()
    """不支持的编解码器。"""

    CORRUPTED_INPUT = auto()
    """输入文件损坏。"""

    CANCELLED = auto()
    """被信号或外部请求取消。"""

    UNKNOWN = auto()
    """无法归类的未知错误。"""


@dataclass(slots=True)
class FfmpegError:
    """封装的 FFmpeg 错误信息。

    :param return_code: 子进程退出码。
    :param stderr: 子进程标准错误输出(解码后的字符串)。
    :param error_type: 分类后的错误类型。
    """

    return_code: int
    stderr: str
    error_type: FfmpegErrorType


def classify_ffmpeg_error(return_code: int, stderr: str) -> FfmpegErrorType:
    """根据退出码和 stderr 分类 FFmpeg 错误。

    分类规则按优先级从高到低:
    - return_code==255 + 文件不存在模式 → MISSING_BINARY
    - 磁盘空间相关关键字 → DISK_FULL
    - 权限相关关键字 → PERMISSION_DENIED
    - 无效参数/未知选项 → INVALID_ARGUMENT
    - 网络瞬时错误关键字 → TRANSIENT_NETWORK
    - HTTP 4xx/5xx 关键字 → UPSTREAM_UNAVAILABLE
    - 损坏输入关键字 → CORRUPTED_INPUT
    - 编解码器不存在 → UNSUPPORTED_CODEC
    - 取消信号关键字 → CANCELLED
    - 默认 → UNKNOWN

    :param return_code: 子进程退出码。
    :param stderr: 子进程标准错误输出(已解码的字符串)。
    :returns: 分类后的错误类型。
    """
    stderr_lower = stderr.lower()

    # MISSING_BINARY: return_code 255 + 文件不存在模式
    if return_code == 255 and ("no such file" in stderr_lower or "not found" in stderr_lower):
        return FfmpegErrorType.MISSING_BINARY

    # DISK_FULL: 磁盘空间不足
    if "no space left" in stderr_lower or "disk full" in stderr_lower:
        return FfmpegErrorType.DISK_FULL

    # PERMISSION_DENIED: 权限拒绝
    if "permission denied" in stderr_lower:
        return FfmpegErrorType.PERMISSION_DENIED

    # INVALID_ARGUMENT: 无效参数或未知选项
    if (
        "invalid argument" in stderr_lower
        or "option not found" in stderr_lower
        or "unrecognized option" in stderr_lower
    ):
        return FfmpegErrorType.INVALID_ARGUMENT

    # TRANSIENT_NETWORK: 瞬时网络错误
    if any(
        keyword in stderr_lower
        for keyword in (
            "connection refused",
            "connection reset",
            "network is unreachable",
            "name or service not known",
            "failed to resolve",
            "timed out",
            "timeout",
        )
    ):
        return FfmpegErrorType.TRANSIENT_NETWORK

    # UPSTREAM_UNAVAILABLE: HTTP 4xx/5xx
    if any(
        keyword in stderr_lower
        for keyword in (
            "server returned 4",
            "server returned 5",
            "http error 5",
            "http error 4",
        )
    ):
        return FfmpegErrorType.UPSTREAM_UNAVAILABLE

    # CORRUPTED_INPUT: 输入文件损坏
    if any(
        keyword in stderr_lower
        for keyword in (
            "invalid data found when processing input",
            "corrupt",
            "moov atom not found",
        )
    ):
        return FfmpegErrorType.CORRUPTED_INPUT

    # UNSUPPORTED_CODEC: 不支持的编解码器
    if any(
        keyword in stderr_lower
        for keyword in (
            "codec not found",
            "encoder not found",
            "decoder not found",
            "unsupported codec",
            "unknown encoder",
            "unknown decoder",
        )
    ):
        return FfmpegErrorType.UNSUPPORTED_CODEC

    # CANCELLED: 被信号/外部请求取消
    if "immediate exit requested" in stderr_lower or (
        return_code == -9 or return_code == 128 + 9  # noqa: PLR2004
    ):
        return FfmpegErrorType.CANCELLED
    # SIGTERM (128 + 15) 也视为取消
    if ("terminated" in stderr_lower and ("signal" in stderr_lower or "sigterm" in stderr_lower)) or (
        return_code == -15 or return_code == 128 + 15
    ):  # noqa: PLR2004
        return FfmpegErrorType.CANCELLED

    return FfmpegErrorType.UNKNOWN


def is_retryable(error_type: FfmpegErrorType) -> bool:
    """判断给定错误类型是否可重试。

    可重试(索引退避):
    - TRANSIENT_NETWORK
    - UPSTREAM_UNAVAILABLE

    永久失败:
    - DISK_FULL, MISSING_BINARY, PERMISSION_DENIED,
      INVALID_ARGUMENT, UNSUPPORTED_CODEC, CORRUPTED_INPUT,
      CANCELLED, UNKNOWN

    :param error_type: 已分类的错误类型。
    :returns: 是否可通过重试恢复。
    """
    return error_type in (
        FfmpegErrorType.TRANSIENT_NETWORK,
        FfmpegErrorType.UPSTREAM_UNAVAILABLE,
    )
