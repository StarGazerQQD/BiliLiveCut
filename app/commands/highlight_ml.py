"""CLI 子命令 — 高光模型状态。"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

console = Console()


def cmd_highlight_model_status() -> None:
    """显示高光模型模式、Schema 与 Champion/Shadow 状态。"""
    from app.analysis.highlight_ml.online import get_online_status

    status = get_online_status()
    table = Table(title="高光模型状态")
    table.add_column("项目")
    table.add_column("值")
    for key in (
        "mode",
        "available",
        "generation",
        "champion_version",
        "champion_model_type",
        "champion_threshold",
        "shadow_version",
        "schema_version",
        "schema_fingerprint",
        "registry_root",
        "error",
    ):
        table.add_row(key, str(status.get(key)))
    console.print(table)


def cmd_highlight_model_train(
    as_shadow: bool = typer.Option(True, "--shadow/--unassigned", help="已有 Champion 时把新版本设为 Shadow"),
    include_xgboost: bool = typer.Option(True, "--xgboost/--no-xgboost", help="是否比较可选 XGBoost"),
    min_samples: int = typer.Option(20, min=1, help="最少人工标签数"),
    min_positive: int = typer.Option(5, min=1, help="最少正标签数"),
    blind_review_limit: int = typer.Option(100, min=0, help="随版本导出的盲审项数"),
) -> None:
    """从当前数据库训练、比较并原子注册新模型版本。"""
    from app.analysis.highlight_ml.operations import train_and_register
    from app.analysis.highlight_ml.training import TrainingConfig
    from app.core.config import settings
    from app.db.session import get_session

    try:
        with get_session() as db:
            result = train_and_register(
                db,
                registry_root=settings.highlight_ml_registry_root,
                config=TrainingConfig(
                    min_samples=min_samples,
                    min_positive=min_positive,
                    include_xgboost=include_xgboost,
                ),
                as_shadow=as_shadow,
                blind_review_limit=blind_review_limit,
            )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        console.print(f"[red]训练或注册失败:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print_json(json.dumps(result.to_dict(), ensure_ascii=False))


def cmd_highlight_model_set_shadow(
    version: int = typer.Argument(..., help="模型版本；0 表示清空 Shadow"),
) -> None:
    """设置或清空 Shadow 版本。"""
    from app.analysis.highlight_ml.registry import ModelRegistry
    from app.analysis.highlight_ml.schema import DEFAULT_FEATURE_SCHEMA
    from app.core.config import settings

    registry = ModelRegistry(settings.highlight_ml_registry_root, schema_fingerprint=DEFAULT_FEATURE_SCHEMA.fingerprint)
    try:
        registry.set_shadow(version if version > 0 else None)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        console.print(f"[red]设置 Shadow 失败:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Shadow 已设置为[/green] {version if version > 0 else 'None'}")


def cmd_highlight_model_promote() -> None:
    """原子提升当前 Shadow 为 Champion。"""
    from app.analysis.highlight_ml.registry import ModelRegistry
    from app.analysis.highlight_ml.schema import DEFAULT_FEATURE_SCHEMA
    from app.core.config import settings

    registry = ModelRegistry(settings.highlight_ml_registry_root, schema_fingerprint=DEFAULT_FEATURE_SCHEMA.fingerprint)
    try:
        version = registry.promote_shadow()
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        console.print(f"[red]提升失败:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Champion 已切换到[/green] v{version}")


def cmd_highlight_model_rollback(
    version: int = typer.Argument(..., min=1, help="要恢复为 Champion 的历史版本"),
) -> None:
    """把 Champion 原子回滚到指定真实历史产物。"""
    from app.analysis.highlight_ml.registry import ModelRegistry
    from app.analysis.highlight_ml.schema import DEFAULT_FEATURE_SCHEMA
    from app.core.config import settings

    registry = ModelRegistry(settings.highlight_ml_registry_root, schema_fingerprint=DEFAULT_FEATURE_SCHEMA.fingerprint)
    try:
        registry.load_artifact(version)
        registry.rollback(version)
    except (KeyError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        console.print(f"[red]回滚失败:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Champion 已回滚到[/green] v{version}")


def cmd_highlight_model_drift(
    limit: int = typer.Option(500, min=1, max=5000, help="最多读取的近期预测数"),
    min_recent_samples: int = typer.Option(20, min=1, help="最少近期样本数"),
) -> None:
    """检查当前 Champion 相对训练基线的线上漂移。"""
    from app.analysis.highlight_ml.operations import check_champion_drift
    from app.core.config import settings
    from app.db.session import get_session

    try:
        with get_session() as db:
            report = check_champion_drift(
                db,
                registry_root=settings.highlight_ml_registry_root,
                limit=limit,
                min_recent_samples=min_recent_samples,
            )
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        console.print(f"[red]漂移检查失败:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print_json(json.dumps(report, ensure_ascii=False))


HIGHLIGHT_ML_COMMANDS = [
    ("highlight-model-status", cmd_highlight_model_status, None),
    ("highlight-model-train", cmd_highlight_model_train, None),
    ("highlight-model-shadow", cmd_highlight_model_set_shadow, None),
    ("highlight-model-promote", cmd_highlight_model_promote, None),
    ("highlight-model-rollback", cmd_highlight_model_rollback, None),
    ("highlight-model-drift", cmd_highlight_model_drift, None),
]
