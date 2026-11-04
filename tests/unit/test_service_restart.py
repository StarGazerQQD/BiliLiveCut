from __future__ import annotations

from config.service_restart import RestartBudget, ServiceReadiness


def test_restart_budget_bounds_repeated_startup_and_short_lived_failures() -> None:
    budget = RestartBudget()
    assert budget.failed() == 1
    budget.observe(ready=True, now=10)
    assert budget.failed() == 2
    budget.observe(ready=True, now=20)
    budget.observe(ready=False, now=90)
    assert budget.failed() == 4
    assert budget.failed() is None


def test_only_continuously_ready_runtime_resets_budget_excluding_cleanup_time() -> None:
    budget = RestartBudget(failures=2)
    budget.observe(ready=True, now=100)
    budget.ended()
    assert budget.failed() == 4
    budget.observe(ready=True, now=2000)
    budget.observe(ready=True, now=2060)
    budget.ended()
    assert budget.failed() == 1


def test_confirmed_stable_runtime_resets_budget_before_http_is_lost() -> None:
    budget = RestartBudget(failures=3)
    budget.observe(ready=True, now=100)
    budget.observe(ready=True, now=159)
    assert budget.failures == 3
    budget.observe(ready=True, now=160)
    assert budget.failures == 0
    budget.observe(ready=False, now=161)
    budget.ended()
    assert budget.failed() == 1


def test_startup_deadline_does_not_restart_after_each_failed_probe() -> None:
    readiness = ServiceReadiness(started_at=100)
    assert readiness.observe(ready=False, now=100) is None
    assert readiness.observe(ready=False, now=159.9) is None
    assert readiness.observe(ready=False, now=160) == "服务首次 HTTP 就绪检查超时"


def test_runtime_http_failure_has_its_own_continuous_deadline() -> None:
    readiness = ServiceReadiness(started_at=100)
    assert readiness.observe(ready=True, now=110) is None
    assert readiness.observe(ready=False, now=1000) is None
    assert readiness.observe(ready=False, now=1059.9) is None
    assert readiness.observe(ready=True, now=1060) is None
    assert readiness.observe(ready=False, now=2000) is None
    assert readiness.observe(ready=False, now=2059.9) is None
    assert readiness.observe(ready=False, now=2060) == "服务持续失去 HTTP 响应"
