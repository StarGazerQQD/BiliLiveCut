#!/usr/bin/env python3
"""Local CI gate — replicate CI checks from the command line.

按顺序复现 CI lint + audit + test + portable 全部检查。
任一步骤失败立即停止 (fail-closed)。

用法:
    python scripts/ci_gate.py
    python scripts/ci_gate.py --skip-portable
    python scripts/ci_gate.py --skip-coverage
    python scripts/ci_gate.py --skip-audit
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _run(cmd: list[str], cwd: str | None = None, desc: str = "") -> bool:
    """Run a command and report success/failure.

    :param cmd: Command and args list.
    :param cwd: Working directory (default: REPO_ROOT).
    :param desc: Human-readable description.
    :returns: True if command succeeded (exit 0).
    """
    print(f"\n{YELLOW}[{desc}]{RESET}")
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd or str(REPO_ROOT))
    if result.returncode == 0:
        print(f"{GREEN}  PASS{RESET}")
        return True
    else:
        print(f"{RED}  FAIL (exit={result.returncode}){RESET}")
        return False


def _pytest(path: str, extra_args: list[str] | None = None, desc: str = "") -> bool:
    """Run pytest on a path.

    :param path: Test path.
    :param extra_args: Additional pytest arguments.
    :param desc: Description string.
    :returns: True if all tests passed.
    """
    cmd = [sys.executable, "-m", "pytest", path, "-v", "--timeout=120"]
    if extra_args:
        cmd.extend(extra_args)
    return _run(cmd, desc=desc or f"pytest {path}")


def main() -> int:
    """Entry point for local CI gate.

    :returns: 0 if all checks pass, 1 if any fail.
    """
    skip_portable = "--skip-portable" in sys.argv
    skip_coverage = "--skip-coverage" in sys.argv
    skip_audit = "--skip-audit" in sys.argv

    print("=" * 60)
    print("  BiliLiveCut Local CI Gate")
    print("=" * 60)
    print(f"  Python: {sys.version}")
    print(f"  Root:   {REPO_ROOT}")
    print("=" * 60)

    all_ok = True

    # ── 1. Ruff check ──
    all_ok &= _run(["ruff", "check", "."], desc="1/8 ruff check")

    # ── 2. Ruff format ──
    all_ok &= _run(["ruff", "format", "--check", "."], desc="2/8 ruff format check")

    # ── 3. Version consistency ──
    all_ok &= _run(
        [sys.executable, "scripts/check_version_consistency.py"],
        desc="3/8 version consistency",
    )

    # ── 4. Release audit ──
    all_ok &= _run(
        [sys.executable, "scripts/release_audit.py", "--quick"],
        desc="4/8 release audit",
    )

    # ── 5. pip-audit ──
    if not skip_audit:
        pip_ok = True
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip_audit", "--strict", "--skip-editable"],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            if r.returncode != 0:
                # Check exemptions
                log_path = REPO_ROOT / "pip-audit.log"
                log_path.write_text(r.stdout + "\n" + r.stderr, encoding="utf-8")
                print(f"{YELLOW}  pip-audit found vulnerabilities (exit={r.returncode}){RESET}")
                r2 = subprocess.run(
                    [sys.executable, "scripts/check_pip_audit_exemptions.py", str(log_path)],
                    capture_output=True,
                    text=True,
                    cwd=str(REPO_ROOT),
                )
                print(r2.stdout.strip())
                if r2.returncode != 0:
                    print(f"{RED}  pip-audit: unexempted vulnerabilities{RESET}")
                    pip_ok = False
                else:
                    print(f"{GREEN}  pip-audit: all covered by exemptions{RESET}")
            else:
                print(f"{GREEN}  pip-audit: clean{RESET}")
        except ImportError:
            print(f"{YELLOW}  pip-audit: not installed (install with: pip install pip-audit){RESET}")
            pip_ok = True  # skip if not installed
        all_ok &= pip_ok

    # ── 6. Main tests + coverage ──
    cov_args = ["--cov=app", "--cov-report=term-missing", "--cov-fail-under=50"]
    if skip_coverage:
        cov_args = []
    all_ok &= _pytest(
        "tests/",
        extra_args=cov_args,
        desc="6/8 pytest tests/ (coverage >= 50%)",
    )

    # ── 7. Portable tests ──
    if not skip_portable:
        all_ok &= _pytest(
            "packaging/portable/tests/",
            desc="7/8 pytest packaging/portable/tests/",
        )

    # ── 8. Final judgment ──
    print(f"\n{'=' * 60}")
    if all_ok:
        print(f"{GREEN}  ALL CHECKS PASSED — CI gate open{RESET}")
    else:
        print(f"{RED}  SOME CHECKS FAILED — CI gate closed{RESET}")
    print(f"{'=' * 60}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
