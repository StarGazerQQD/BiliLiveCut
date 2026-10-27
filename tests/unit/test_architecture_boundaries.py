from pathlib import Path

import pytest

from scripts.check_architecture import check, violations


@pytest.mark.parametrize(
    "path,source",
    [
        ("app/analysis/editorial_contracts.py", "from app.web import main"),
        ("app/core/notifications.py", "from app.db import session"),
        ("app/db/notifications.py", "from app.web import main"),
        ("app/models/contracts.py", "from app.core import config"),
        ("app/models/configuration.py", "from app.web.services import settings"),
        ("app/core/configuration_policy.py", "from app.web import main"),
        ("app/core/supervisor.py", "from app.pipeline import task_worker"),
        ("app/core/execution.py", "from app.jobs import manager"),
        ("app/recording/schedule_runner.py", "from app.web.services import rooms"),
        ("app/jobs/contracts.py", "from app.db import session"),
        ("app/jobs/manager.py", "import app.web.main as hidden"),
        ("app/jobs/manager.py", "def handler():\n    from ..web import main"),
        ("app/jobs/context.py", "from app.recording.imports import prepare_import"),
        ("app/db/job_store.py", "from app.pipeline.orchestrator import produce_clip"),
    ],
)
def test_boundary_checker_rejects_reverse_dependencies(path: str, source: str) -> None:
    assert violations(path, source)


def test_boundary_checker_accepts_store_contract_and_type_dependencies() -> None:
    assert (
        violations("app/jobs/manager.py", "from app.db import job_store\nfrom app.jobs.context import JobContext") == []
    )
    assert violations("app/jobs/contracts.py", "from pydantic import JsonValue") == []


def test_actual_task_architecture_respects_boundaries() -> None:
    assert check() == []


def test_boundary_checker_rejects_missing_contract(tmp_path: Path) -> None:
    assert any("contracts.py" in finding and "缺失" in finding for finding in check(tmp_path))
