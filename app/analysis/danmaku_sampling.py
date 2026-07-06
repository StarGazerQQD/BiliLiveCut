"""弹幕分级采样 (V0.1.12.9)。

按弹幕类型、价值分层对弹幕进行采样, 在高频直播中限制入库量防止数据库膨胀。

采样策略:
- DANMAKU (普通弹幕): 保留率 30% (高频可适当丢弃)
- SUPER_CHAT (SC / 醒目留言): 保留率 100% (高价值互动)
- GUARD (舰长/提督/总督): 保留率 100% (付费用户行为)
- GIFT (礼物): 保留率 80%
- OTHER: 保留率 50%

每房间维护滑动窗口缓冲区, 按窗口内密度动态调整丢弃率:
- 窗口内弹幕数 > 1000/分钟 → 普通弹幕保留率降至 10%
- 窗口内弹幕数 > 200/分钟 → 普通弹幕保留率 30% (默认)
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from app.db.models import DanmakuType

# 基础保留率
_BASE_RETAIN_RATE: dict[DanmakuType, float] = {
    DanmakuType.DANMAKU: 0.30,
    DanmakuType.SUPERCHAT: 1.0,
    DanmakuType.INTERACT: 1.0,
    DanmakuType.GIFT: 0.80,
    DanmakuType.OTHER: 0.50,
}

# 密度阈值 (每分钟条数)
_HIGH_DENSITY_THRESHOLD = 1000  # 超高密度
_MEDIUM_DENSITY_THRESHOLD = 200  # 中高密度

# 超高密度下普通弹幕保留率
_HIGH_DENSITY_RETAIN = 0.10


@dataclass
class DanmakuSampler:
    """弹幕分级采样器。

    维护一个滑动窗口 (默认 60 秒), 根据窗口内弹幕密度动态调整采样率。

    :param window_s: 采样窗口长度 (秒)。
    """

    window_s: float = 60.0
    _timestamps: deque[float] = field(default_factory=deque)
    _seq: int = 0  # 确定性 hash seed 序列号

    def should_keep(self, dm_type: DanmakuType) -> bool:
        """判断一条弹幕是否应被保留入库。

        :param dm_type: 弹幕类型。
        :returns: True 表示保留。
        """
        # 确定性 hash (基于序列号, 避免 random 的不可复现)
        self._seq += 1
        # 简单分桶: 用 seq % 100 作为 0-99 的均匀分布
        bucket = (self._seq * 2654435761) % 100  # Knuth multiplicative hash
        rate = self._effective_rate(dm_type)
        return bucket < int(rate * 100)

    def _effective_rate(self, dm_type: DanmakuType) -> float:
        """根据当前窗口密度计算实际保留率。

        :param dm_type: 弹幕类型。
        :returns: 实际保留率 (0.0-1.0)。
        """
        base = _BASE_RETAIN_RATE.get(dm_type, 0.50)
        if dm_type != DanmakuType.DANMAKU:
            return base
        # 仅普通弹幕受密度影响
        density = self._density_per_minute()
        if density > _HIGH_DENSITY_THRESHOLD:
            return _HIGH_DENSITY_RETAIN
        return base

    def record(self) -> None:
        """记录一条弹幕进入窗口 (无论最终是否入库)。"""
        now = time.time()
        self._timestamps.append(now)
        self._prune()

    def _density_per_minute(self) -> float:
        """计算滑动窗口内每分钟弹幕数。

        :returns: 每分钟弹幕数。
        """
        self._prune()
        if not self._timestamps:
            return 0.0
        elapsed = max(self._timestamps[-1] - self._timestamps[0], 1.0)
        return len(self._timestamps) / elapsed * 60

    def _prune(self) -> None:
        """丢弃窗口外的旧时间戳。"""
        cutoff = time.time() - self.window_s
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()


# 按房间维护采样器实例
_room_samplers: dict[int, DanmakuSampler] = {}


def get_sampler(room_id: int) -> DanmakuSampler:
    """获取或创建房间级弹幕采样器。

    :param room_id: Bilibili 房间号。
    :returns: 该房间的 :class:`DanmakuSampler` 实例。
    """
    if room_id not in _room_samplers:
        _room_samplers[room_id] = DanmakuSampler()
    return _room_samplers[room_id]
