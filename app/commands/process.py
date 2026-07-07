"""CLI 子命令 — 处理命令。"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table
from sqlmodel import select

from app.db.models import FinalClip, HighlightCandidate
from app.db.session import get_session

console = Console()


def cmd_transcribe(
    segment_id: int = typer.Argument(..., help="raw_segments 主键"),
) -> None:
    """转写指定片段(阶段2,需安装 .[asr])。

    :param segment_id: ``raw_segments`` 主键。
    """
    from app.analysis.transcribe import transcribe_segment

    t = transcribe_segment(segment_id)
    console.print(f"[green]转写完成[/green] transcript_id={t.id} 语言={t.language}")
    console.print(t.text[:500] or "(空)")


def cmd_score(
    segment_id: int = typer.Argument(..., help="raw_segments 主键(需已转写)"),
) -> None:
    """对已转写片段做高光评分(阶段2)。

    :param segment_id: ``raw_segments`` 主键。
    """
    from app.analysis.highlight import score_segment

    candidate = score_segment(segment_id)
    if candidate is None:
        console.print("[yellow]未达阈值或重复,未生成候选。[/yellow]")
    else:
        console.print(
            f"[green]★ 生成高光候选[/green] id={candidate.id} "
            f"分数={candidate.highlight_score:.3f} 理由={candidate.reason}"
        )


def cmd_process(
    segment_id: int = typer.Argument(..., help="raw_segments 主键"),
) -> None:
    """对单个片段执行完整分析流程:转写 + 高光评分(阶段2)。

    :param segment_id: ``raw_segments`` 主键。
    """
    from app.pipeline.orchestrator import process_segment_sync

    candidate = process_segment_sync(segment_id)
    if candidate is None:
        console.print("[yellow]处理完成,未生成候选。[/yellow]")
    else:
        console.print(f"[green]★ 处理完成并生成候选[/green] id={candidate.id} 分数={candidate.highlight_score:.3f}")


def cmd_list_candidates(
    limit: int = typer.Option(20, help="最多显示数量"),
) -> None:
    """列出高光候选。

    :param limit: 显示上限。
    """
    with get_session() as db:
        rows = db.exec(
            select(HighlightCandidate).order_by(HighlightCandidate.highlight_score.desc())  # type: ignore[attr-defined]
        ).all()

    rows = rows[:limit]
    if not rows:
        console.print("[yellow]暂无高光候选。[/yellow]")
        return

    table = Table(title="高光候选(按分数降序)")
    for col in ("id", "session", "score", "rule", "llm", "status", "reason"):
        table.add_column(col)
    for c in rows:
        table.add_row(
            str(c.id),
            str(c.session_id),
            f"{c.highlight_score:.3f}",
            f"{c.rule_score:.3f}",
            f"{c.llm_score:.3f}",
            c.status,
            (c.reason or "")[:40],
        )
    console.print(table)


def cmd_clip(
    candidate_id: int = typer.Argument(..., help="highlight_candidates 主键"),
) -> None:
    """把一个高光候选生成为成品 MP4(阶段3,不含文案)。

    :param candidate_id: ``highlight_candidates`` 主键。
    """
    from app.clipping.clipper import produce_clip as make_clip

    c = make_clip(candidate_id)
    console.print(f"[green]切片完成[/green] clip_id={c.id} 时长={c.duration_s:.1f}s -> {c.file_path}")


def cmd_copywrite(
    clip_id: int = typer.Argument(..., help="final_clips 主键"),
) -> None:
    """为成品切片生成标题/简介/标签等文案(阶段3)。

    :param clip_id: ``final_clips`` 主键。
    """
    from app.publishing.copywriter import generate_copy

    c = generate_copy(clip_id)
    console.print(f"[green]文案完成[/green] 状态={c.status}\n标题: {c.title}")
    console.print(f"简介: {c.description}")


def cmd_produce(
    candidate_id: int = typer.Argument(..., help="highlight_candidates 主键"),
) -> None:
    """对一个候选执行完整出片流程:切片 + 文案(阶段3)。

    :param candidate_id: ``highlight_candidates`` 主键。
    """
    from app.pipeline.orchestrator import process_candidate

    c = process_candidate(candidate_id)
    if c is None:
        console.print("[red]出片失败,详见日志。[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]★ 出片完成[/green] clip_id={c.id} 状态={c.status}\n标题: {c.title}\n文件: {c.file_path}")


def cmd_list_clips(limit: int = typer.Option(20, help="最多显示数量")) -> None:
    """列出成品切片。

    :param limit: 显示上限。
    """
    with get_session() as db:
        rows = db.exec(
            select(FinalClip).order_by(FinalClip.created_at.desc())  # type: ignore[attr-defined]
        ).all()
    rows = rows[:limit]
    if not rows:
        console.print("[yellow]暂无成品切片。[/yellow]")
        return
    table = Table(title="成品切片")
    for col in ("id", "candidate", "status", "dur", "title", "file"):
        table.add_column(col)
    for c in rows:
        table.add_row(
            str(c.id),
            str(c.candidate_id),
            c.status,
            f"{c.duration_s:.0f}s" if c.duration_s else "-",
            (c.title or "")[:30],
            c.file_path.split("\\")[-1].split("/")[-1],
        )
    console.print(table)


# 注册列表
PROCESS_COMMANDS = [
    ("transcribe", cmd_transcribe, None),
    ("score", cmd_score, None),
    ("process", cmd_process, None),
    ("list-candidates", cmd_list_candidates, None),
    ("clip", cmd_clip, None),
    ("copywrite", cmd_copywrite, None),
    ("produce", cmd_produce, None),
    ("list-clips", cmd_list_clips, None),
]
