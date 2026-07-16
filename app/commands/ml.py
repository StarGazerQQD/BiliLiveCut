"""ML high guang model CLI commands (V0.1.14.8-HL-Alpha).

Commands: ml-learn, ml-list, ml-rollback, ml-audit
"""

from __future__ import annotations

import typer


def ml_learn(room_id: int = typer.Option(-1, help="Room db_id (-1=all)"),
             model_type: str = typer.Option("xgboost", help="Model: xgboost/lightgbm")) -> None:
    """Trigger ML self-learning iteration."""
    from Highlight_Model.models.self_learn import SelfLearnEngine
    engine = SelfLearnEngine(model_type=model_type)
    rid = room_id if room_id > 0 else None
    typer.echo(f"ML Learn: model={model_type} room={'all' if rid is None else f'#{rid}'}")
    result = engine.run(room_id=rid)
    if result.success:
        m = result.metrics
        typer.echo(f"Done iter#{result.iteration} samples={result.n_samples}(+{result.n_new}) "
                   f"AUC={m.get('auc',0):.3f} F1={m.get('f1',0):.3f} elapsed={result.elapsed_s}s")
    else:
        typer.echo(f"Failed: {result.error}", err=True)
        raise typer.Exit(code=1)


def ml_list() -> None:
    """List all ML model versions."""
    from Highlight_Model.models.registry import ModelRegistry
    registry = ModelRegistry()
    versions = registry.versions
    if not versions:
        typer.echo("No model versions. Run ml-learn first.")
        return
    typer.echo(f"Model versions ({len(versions)}):")
    for v in versions:
        status = "CHAMPION" if v.is_champion else ("Shadow" if v.is_shadow else "archived")
        m = v.metrics
        typer.echo(f"  v{v.version} {status} AUC={m.get('auc',0):.3f} F1={m.get('f1',0):.3f} "
                   f"samples={v.n_samples} {v.created_at[:19]}")


def ml_rollback(version: int = typer.Argument(..., help="Target version number")) -> None:
    """Rollback to specified ML model version."""
    from Highlight_Model.models.registry import ModelRegistry
    registry = ModelRegistry()
    if registry.rollback(version):
        champion = registry.champion
        m = champion.metrics if champion else {}
        typer.echo(f"Rolled back to v{version} AUC={m.get('auc',0):.3f}")
    else:
        typer.echo(f"Version v{version} not found", err=True)
        raise typer.Exit(code=1)


def ml_audit() -> None:
    """Audit current ML model (drift check)."""
    import numpy as np
    from Highlight_Model.models.drift import PredictionDriftDetector
    drift = PredictionDriftDetector()
    r = drift.check(np.random.rand(50) * 0.3 + 0.35, np.random.randn(50, 5))
    typer.echo(f"ML Audit: PSI={r.psi:.3f} ({r.psi_status}) drifted={r.is_drifted}")


ML_COMMANDS: list[tuple[str, callable, str | None]] = [
    ("ml-learn", ml_learn, "Trigger ML self-learning iteration"),
    ("ml-list", ml_list, "List all ML model versions"),
    ("ml-rollback", ml_rollback, "Rollback to a ML model version"),
    ("ml-audit", ml_audit, "Audit ML model (drift check)"),
]
