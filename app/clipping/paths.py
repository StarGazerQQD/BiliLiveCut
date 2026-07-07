"""Path utilities for clip rendering — lease partial, final, backup paths.

All formal paths are keyed by (event_id, variant_type, render_config_hash),
NOT just by candidate_id, to support multi-variant, multi-config rendering.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.core.paths import clips_dir


def build_lease_partial_path(task_id: int, lease_token: str) -> str:
    """生成租约专属临时渲染文件路径。

    格式: clips_dir/clip.{task_id}.{lease_token[:8]}.partial.mp4

    :param task_id: SegmentTask ID。
    :param lease_token: 租约令牌 (UUID hex)。
    :returns: 临时文件绝对路径。
    """
    return str(Path(clips_dir()) / f"clip.{task_id}.{lease_token[:8]}.partial.mp4")


def build_final_clip_path(
    event_id: int,
    variant_type: str,
    render_config_hash: str,
) -> str:
    """生成正式切片文件路径 (以 event + variant + config 为键)。

    格式: clips_dir/clip_{event_id}_{variant_type}_{render_config_hash[:8]}.mp4

    :param event_id: HighlightEvent ID。
    :param variant_type: ClipVariantType 值 (single, full_context, etc.)。
    :param render_config_hash: 渲染配置指纹 (SHA-256, 取前8位)。
    :returns: 正式文件绝对路径。
    """
    short_hash = render_config_hash[:8] if render_config_hash else "default"
    return str(Path(clips_dir()) / f"clip_{event_id}_{variant_type}_{short_hash}.mp4")


def build_backup_path(variant_id: int, generation: int = 1) -> str:
    """生成旧正式文件的备份路径 (替换前备份)。

    格式: clips_dir/clip_backup_{variant_id}_gen{generation}_{ts}.bak

    :param variant_id: ClipVariant ID。
    :param generation: 当前 generation 编号。
    :returns: 备份文件绝对路径。
    """
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return str(Path(clips_dir()) / f"clip_backup_{variant_id}_gen{generation}_{ts}.bak")
