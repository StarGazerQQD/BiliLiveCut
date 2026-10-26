# ruff: noqa: D100
from __future__ import annotations

from pydantic import JsonValue

from app.analysis.reanalysis_targets import ReanalysisSelection, preview_reanalysis
from app.core.execution import run_sync
from app.db.session import get_session
from app.jobs.contracts import JobData
from app.jobs.manager import JobManager


def _validated_preview(selection: ReanalysisSelection, fingerprint: str) -> dict[str, JsonValue]:
    with get_session() as db:
        preview = preview_reanalysis(db, selection)
    if preview["fingerprint"] != fingerprint:
        raise ValueError("预览已过期，请重新核对分析对象")
    targets = preview["targets"]
    if not isinstance(targets, list) or not targets:
        raise ValueError("当前筛选没有可以重分析的对象")
    if all(item["protection"] is not None for item in targets):
        raise ValueError("全部对象均受人工或下游资产保护")
    return {"session_id": selection.session_id, "targets": targets}


async def enqueue_editorial_reanalysis(
    manager: JobManager,
    selection: ReanalysisSelection,
    fingerprint: str,
    *,
    actor: str,
) -> JobData:
    """仅接受服务端当前预览，重复点击共用已有活动作业。"""
    payload = await run_sync(lambda: _validated_preview(selection, fingerprint))
    return await manager.enqueue(
        "editorial_reanalysis", payload, label="重新分析编辑判断", owner=actor, dedup_key=f"editorial:{fingerprint}"
    )
