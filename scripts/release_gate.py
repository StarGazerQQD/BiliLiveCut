#!/usr/bin/env python3
"""Unified release gate — 本地可调用的发布门禁命令。

按顺序执行 Release 发布前的全部检查:
  1. release_audit (完整审计)
  2. version_consistency (版本一致性)
  3. Payload 构建 + Manifest/ZIP 契约验证
  4. Portable 全部测试
  5. 主线全部测试
  6. Ruff lint + format check

任一步骤失败立即停止 (fail-closed)。

用法:
    python scripts/release_gate.py
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

    :param cmd: Command and args.
    :param cwd: Working directory.
    :param desc: Human-readable description.
    :returns: True if command succeeded.
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


def _pytest(path: str, desc: str = "") -> bool:
    """Run pytest on a path."""
    return _run(
        [sys.executable, "-m", "pytest", path, "-q", "--timeout=120", "--fail-on-skip"],
        desc=desc or f"pytest {path}",
    )


def main() -> int:  # noqa: D103
    print("=" * 60)
    print("  BiliLiveCut Release Gate")
    print("=" * 60)

    all_ok = True

    # ── Step 1: release_audit ──
    all_ok &= _run([sys.executable, "scripts/release_audit.py"], desc="1/7 release_audit")

    # ── Step 2: version_consistency ──
    all_ok &= _run([sys.executable, "scripts/check_version_consistency.py"], desc="2/7 version_consistency")

    # ── Step 3: Ruff ──
    all_ok &= _run([sys.executable, "scripts/run_ruff.py", "check"], desc="3/7 ruff check")
    all_ok &= _run([sys.executable, "scripts/run_ruff.py", "format"], desc="3/7 ruff format check")

    # ── Step 4: Payload 构建 + 契约验证 ──
    all_ok &= _run([sys.executable, "packaging/portable/build_payload.py"], desc="4/7 build_payload")

    # Payload contract cross-verify
    payload_ok = True
    try:
        import hashlib
        import json
        import zipfile

        payload_dir = REPO_ROOT / "packaging/portable/dist/payload"
        m = json.loads((payload_dir / "payload_manifest.json").read_text("utf-8"))
        with zipfile.ZipFile(payload_dir / "source_payload.zip") as zf:
            entries = set(zf.namelist())
            zf_cnt = sum(1 for name in entries if not name.endswith("/"))
        mf_cnt = m["file_count"]
        mf_entries = len(m["files"])
        if zf_cnt != mf_cnt or zf_cnt != mf_entries:
            print(f"{RED}  4/7 Payload contract FAIL: ZIP={zf_cnt} MF={mf_cnt} files={mf_entries}{RESET}")
            payload_ok = False

        abi = m["python_abi"]
        required_native = {
            f"app/analysis/_c_speedups.{abi}-win_amd64.pyd",
            f"app/analysis/_speedups_round2.{abi}-win_amd64.pyd",
            "app/analysis/_rust_cluster.pyd",
        }
        missing_native = sorted(required_native - entries)
        if missing_native:
            print(f"{RED}  4/7 Payload native contract FAIL: missing={missing_native}{RESET}")
            payload_ok = False
        foreign_native = sorted(name for name in entries if name.endswith((".so", ".dll")))
        if foreign_native:
            print(f"{RED}  4/7 Payload native contract FAIL: foreign={foreign_native}{RESET}")
            payload_ok = False

        sums_lines = (payload_dir / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
        for line in sums_lines:
            expected, filename = line.split(maxsplit=1)
            if filename == "SHA256SUMS.txt":
                print(f"{RED}  4/7 Payload checksum FAIL: checksum file lists itself{RESET}")
                payload_ok = False
                continue
            actual = hashlib.sha256((payload_dir / filename).read_bytes()).hexdigest()
            if actual != expected:
                print(f"{RED}  4/7 Payload checksum FAIL: {filename}{RESET}")
                payload_ok = False
        if payload_ok:
            print(f"{GREEN}  4/7 Payload contract OK: {zf_cnt} files{RESET}")
    except Exception as exc:
        print(f"{RED}  4/7 Payload contract FAIL: {exc}{RESET}")
        payload_ok = False
    all_ok &= payload_ok

    # ── Step 5: 主线测试 ──
    all_ok &= _pytest("tests/", desc="5/7 main tests")

    # ── Step 6: Portable 测试 ──
    all_ok &= _pytest("packaging/portable/tests/", desc="6/7 portable tests")

    # ── Step 7: 最终判断 ──
    print(f"\n{'=' * 60}")
    if all_ok:
        print(f"{GREEN}  ALL CHECKS PASSED — ready to release{RESET}")
    else:
        print(f"{RED}  SOME CHECKS FAILED — cannot release{RESET}")
    print(f"{'=' * 60}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
