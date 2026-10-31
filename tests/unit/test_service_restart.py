from __future__ import annotations

from config.service_restart import RestartBudget


def test_restart_budget_bounds_repeated_startup_and_short_lived_failures() -> None:
    budget = RestartBudget()
    assert budget.failed(0) == 1
    budget.observe(ready=True, now=10)
    assert budget.failed(11) == 2
    budget.observe(ready=True, now=20)
    budget.observe(ready=False, now=90)
    assert budget.failed(100) == 4
    assert budget.failed(101) is None


def test_only_continuously_ready_runtime_resets_budget_excluding_cleanup_time() -> None:
    budget = RestartBudget(failures=2)
    budget.observe(ready=True, now=100)
    budget.ended(110)
    assert budget.failed(1000) == 4
    budget.observe(ready=True, now=2000)
    budget.observe(ready=True, now=2060)
    budget.ended(2061)
    assert budget.failed(3000) == 1
