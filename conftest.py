"""Repository-wide pytest release options."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the fail-closed release option."""
    parser.addoption(
        "--fail-on-skip",
        action="store_true",
        default=False,
        help="fail the test session if any test is skipped",
    )


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Turn any skip into a release-gating failure when explicitly requested."""
    if not session.config.getoption("--fail-on-skip"):
        return
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = terminal.stats.get("skipped", []) if terminal is not None else []
    if skipped:
        if terminal is not None:
            terminal.write_sep("=", f"FAIL: {len(skipped)} skipped test(s) are forbidden by --fail-on-skip", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
