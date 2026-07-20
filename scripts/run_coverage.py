#!/usr/bin/env python3
"""Unified CI coverage runner — 与 GitHub Actions 完全一致的覆盖率测量入口。

执行与 CI 相同的 pytest 命令, 解析 coverage.xml 输出精确数字,
诊断 native C 扩展加载状态, 并提供差距分析。

用法:
    python scripts/run_coverage.py

输出示例:
    Statements:     11329
    Covered:         5502
    Missed:          5827
    Coverage:        48.57%
    Target 50%:      需要再覆盖 163 行
    Target 51%:      需要再覆盖 276 行
    Native C ext:    loaded (app.analysis._c_speedups)
    Result:          FAIL (48.57% < 50.00%)

退出码:
    0 = 覆盖率达标
    1 = 覆盖率不达标 (非零退出，与 CI 一致)

要求:
    - 不需要 setuptools,只依赖标准库 (xml.etree)
    - 不需要 BLC_SKIP_C_EXTENSIONS 的补充逻辑（因为检查 native 本身就是诊断，不是决定覆盖率）
    - macOS 允许跳过 C 扩展（通过 BLC_SKIP_C_EXTENSIONS 环境变量），但仍会输出警告
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COVERAGE_XML = REPO_ROOT / "coverage.xml"


def check_native_extension() -> tuple[bool, str]:
    """Check if native C extension is loadable.

    :returns: (loaded, description).
    """
    skip_flag = os.environ.get("BLC_SKIP_C_EXTENSIONS", "")
    if skip_flag and skip_flag != "0":
        return False, f"skipped (BLC_SKIP_C_EXTENSIONS={skip_flag})"

    try:
        import importlib

        importlib.import_module("app.analysis._c_speedups")
        return True, "loaded (app.analysis._c_speedups)"
    except ImportError:
        return False, "NOT FOUND — C extension not compiled; coverage may differ from CI"


def parse_coverage_xml() -> dict | None:
    """Parse coverage.xml and extract exact numbers.

    Coverage.py XML root attributes:
      - lines-valid: total statements
      - lines-covered: covered statements
      - line-rate: coverage ratio (0.0-1.0)

    :returns: dict with {statements, covered, missed, percentage} or None if XML not found.
    """
    if not COVERAGE_XML.exists():
        return None

    try:
        tree = ET.parse(COVERAGE_XML)
        root = tree.getroot()

        statements = int(root.attrib.get("lines-valid", 0))
        covered = int(root.attrib.get("lines-covered", 0))
        missed = statements - covered

        if statements == 0:
            return None

        # line-rate is a decimal 0.0–1.0
        line_rate = float(root.attrib.get("line-rate", 0))
        pct = line_rate * 100

        return {
            "statements": statements,
            "covered": covered,
            "missed": missed,
            "percentage": pct,
        }
    except (ET.ParseError, ValueError, KeyError) as exc:
        print(f"WARNING: failed to parse coverage.xml: {exc}", file=sys.stderr)
        return None


def run_pytest() -> int:
    """Run pytest with coverage, matching CI settings exactly.

    :returns: pytest exit code.
    """
    env = os.environ.copy()
    env.setdefault("ASR_NO_MODEL_DOWNLOAD", "1")

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/",
        "-v",
        "--tb=short",
        "--cov=app",
        "--cov-report=term-missing",
        "--cov-report=xml",
        "--cov-fail-under=50",
    ]

    print(f"Running: {' '.join(cmd)}")
    print("-" * 60)

    result = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env)
    return result.returncode


def main() -> int:
    """Entry point.

    :returns: 0 if coverage >= 50%, 1 otherwise.
    """
    print("=" * 60)
    print("  BiliLiveCut CI Coverage Runner")
    print("=" * 60)

    # ── Native extension check ──
    native_loaded, native_msg = check_native_extension()
    status = "OK" if native_loaded else "WARNING"
    print(f"\n  Native C extension: [{status}] {native_msg}")
    if not native_loaded:
        print("  → Coverage may be overestimated (excluded C lines run in Python fallback)")

    # ── Run pytest ──
    print("\n  Running pytest with coverage...")
    exit_code = run_pytest()

    # ── Parse coverage.xml ──
    data = parse_coverage_xml()
    if data is None:
        print("\n  FATAL: coverage.xml not found or unparseable", file=sys.stderr)
        return exit_code if exit_code != 0 else 1

    statements = data["statements"]
    covered = data["covered"]
    missed = data["missed"]
    pct = data["percentage"]

    # ── Gap analysis ──
    gap_50 = max(0, int(statements * 0.50) - covered + 1)
    gap_51 = max(0, int(statements * 0.51) - covered + 1)

    print()
    print("=" * 60)
    print("  Coverage Diagnostic")
    print("=" * 60)
    print(f"  Statements:     {statements}")
    print(f"  Covered:        {covered}")
    print(f"  Missed:         {missed}")
    print(f"  Coverage:       {pct:.2f}%")
    print(f"  Target 50%:     需要再覆盖 {gap_50} 行")
    print(f"  Target 51%:     需要再覆盖 {gap_51} 行")
    print()

    coverage_passed = pct >= 50.0
    tests_passed = exit_code == 0
    passed = tests_passed and coverage_passed
    print(f"  Tests:          {'PASS' if tests_passed else f'FAIL (pytest exit={exit_code})'}")
    if coverage_passed:
        print(f"  Coverage gate:  PASS ({pct:.2f}% >= 50.00%)")
    else:
        print(f"  Coverage gate:  FAIL ({pct:.2f}% < 50.00%)")
    print(f"  Overall result: {'PASS' if passed else 'FAIL'}")
    print("=" * 60)

    return 0 if passed else (exit_code or 1)


if __name__ == "__main__":
    sys.exit(main())
