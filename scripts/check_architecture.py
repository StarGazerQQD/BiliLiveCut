# ruff: noqa: D100
from __future__ import annotations

import ast
import sys
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOUNDARIES = {
    "app/analysis/editorial_contracts.py": ("app", "sqlalchemy", "sqlmodel", "fastapi", "starlette"),
    "app/core/notifications.py": ("app", "sqlalchemy", "sqlmodel", "fastapi", "starlette"),
    "app/db/notifications.py": ("app.web", "app.pipeline", "app.recording", "fastapi", "starlette"),
    "app/models/contracts.py": ("app", "sqlalchemy", "sqlmodel", "fastapi", "starlette"),
    "app/models/configuration.py": ("app.web", "app.pipeline", "app.clipping", "fastapi", "starlette"),
    "app/core/configuration_policy.py": ("app", "sqlalchemy", "sqlmodel", "fastapi", "starlette"),
    "app/core/supervisor.py": ("app", "sqlalchemy", "sqlmodel", "fastapi", "starlette"),
    "app/core/execution.py": ("app.web", "app.jobs", "app.pipeline", "app.recording", "sqlalchemy", "sqlmodel"),
    "app/recording/schedules.py": ("app.web", "fastapi", "starlette"),
    "app/recording/schedule_runner.py": ("app.web", "fastapi", "starlette"),
    "app/jobs/contracts.py": ("app", "sqlalchemy", "sqlmodel", "fastapi", "starlette"),
    "app/jobs/context.py": ("app.web", "app.recording", "app.clipping", "app.pipeline", "fastapi", "starlette"),
    "app/jobs/manager.py": (
        "app.web",
        "app.recording",
        "app.clipping",
        "app.pipeline",
        "app.publishing",
        "sqlmodel",
        "fastapi",
        "starlette",
    ),
    "app/db/job_store.py": ("app.web", "app.recording", "app.clipping", "app.pipeline", "app.publishing", "fastapi"),
}


def violations(path: str, source: str) -> list[str]:
    """检查明确重构模块的导入方向，覆盖局部导入与相对导入。"""
    forbidden = BOUNDARIES.get(path, ())
    if not forbidden:
        return []
    package = ".".join(Path(path).parts[:-1])
    findings: list[str] = []
    for node in ast.walk(ast.parse(source, filename=path)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = resolve_name("." * node.level + base, package)
            names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        for name in names:
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden):
                findings.append(f"{path}:{node.lineno}: 禁止依赖 {name}")
                break
    return findings


def check(root: Path = ROOT) -> list[str]:
    """检查约定文件必须存在并保持声明的依赖方向。"""
    findings: list[str] = []
    for relative in BOUNDARIES:
        path = root / relative
        if not path.is_file():
            findings.append(f"{relative}: 架构边界文件缺失")
            continue
        findings.extend(violations(relative, path.read_text(encoding="utf-8")))
    return findings


def main() -> int:
    """供本地与 CI 使用同一个架构检查入口。"""
    findings = check()
    for finding in findings:
        print(finding)
    if not findings:
        print(f"架构导入边界通过：{len(BOUNDARIES)} 个模块")
    return int(bool(findings))


if __name__ == "__main__":
    sys.exit(main())
