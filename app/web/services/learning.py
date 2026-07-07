"""Learning (V0.1.14.2)."""

from __future__ import annotations

from typing import Any


def threshold_learning_status(room_id: int) -> dict[str, Any]:
    """返回某房间的阈值自学习状态。

    :param room_id: 直播间 db id。
    :returns: 含样本数、推荐阈值等信息的字典。
    """
    from app.analysis import threshold_learning as tl

    return tl.feedback_summary(room_id)


# --------------------------------------------------------------------------- #
# 录制自动恢复(V0.1.2)
# --------------------------------------------------------------------------- #
