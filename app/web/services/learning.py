"""Learning."""

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


# --------------------------------------------------------------------------- #
# V0.1.14.8-HL: ML 高光模型自学习
# --------------------------------------------------------------------------- #
def ml_learn_status() -> dict[str, Any]:
    """返回 ML 高光模型的自学习状态摘要。"""
    try:
        from Highlight_Model.models.self_learn import SelfLearnEngine
        return SelfLearnEngine().status
    except Exception as exc:
        return {"model_available": False, "error": str(exc)}


def trigger_ml_self_learn(room_id: int | None = None) -> dict[str, Any]:
    """触发一次 ML 高光模型自学习。"""
    try:
        from Highlight_Model.models.self_learn import SelfLearnEngine
        result = SelfLearnEngine().run(room_id=room_id)
        return {
            "success": result.success, "model_path": result.model_path,
            "n_samples": result.n_samples, "n_positive": result.n_positive,
            "n_new": result.n_new, "metrics": result.metrics,
            "iteration": result.iteration, "elapsed_s": result.elapsed_s,
            "error": result.error,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


_last_ml_learn_at: float = 0.0


def _maybe_auto_ml_learn() -> None:
    """如果条件满足，自动触发 ML 重训练。"""
    global _last_ml_learn_at
    try:
        from app.core.config import settings
        if not settings.ml_auto_learn:
            return
        import time
        cooldown_s = settings.ml_auto_learn_cooldown_min * 60
        if time.monotonic() - _last_ml_learn_at < cooldown_s:
            return
        from Highlight_Model.models.self_learn import SelfLearnEngine
        state = SelfLearnEngine()._state
        prev_ids = set(state.get("trained_sample_ids", []))
        try:
            from app.db.models import ThresholdFeedback
            from app.db.session import get_session
            from sqlmodel import select, func
            with get_session() as db:
                total = db.scalar(select(func.count()).select_from(ThresholdFeedback)) or 0
            if total - len(prev_ids) < settings.ml_min_new_samples:
                return
        except Exception:
            pass
        _last_ml_learn_at = time.monotonic()
        import threading
        threading.Thread(target=_run_auto_learn, daemon=True).start()
    except Exception:
        pass


def _run_auto_learn() -> None:
    try:
        from Highlight_Model.models.self_learn import SelfLearnEngine
        from loguru import logger
        result = SelfLearnEngine().run()
        if result.success:
            logger.info("ML 自动重训练完成 #{} AUC={:.3f}", result.iteration, result.metrics.get("auc", 0))
    except Exception:
        pass
