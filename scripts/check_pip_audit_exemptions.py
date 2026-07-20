#!/usr/bin/env python3
"""Validate pip-audit output against exemptions.

Reads pip-audit log from stdin or file argument, checks all found
vulnerabilities against scripts/pip-audit-exemptions.json.

Exit code:
    0 = all vulnerabilities covered by exemptions (or no vulnerabilities)
    1 = unexempted vulnerabilities exist
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXEMPTIONS_PATH = REPO_ROOT / "scripts" / "pip-audit-exemptions.json"


def load_exemptions() -> dict:
    """Load the exemptions file.

    :returns: Exemptions dict.
    :raises FileNotFoundError: If exemptions file missing.
    """
    if not EXEMPTIONS_PATH.exists():
        raise FileNotFoundError(f"Exemptions file not found: {EXEMPTIONS_PATH}")
    return json.loads(EXEMPTIONS_PATH.read_text(encoding="utf-8"))


def parse_vulnerabilities(log_path: str) -> list[dict]:
    """Parse pip-audit output to extract vulnerability entries.

    :param log_path: Path to pip-audit log file.
    :returns: List of {name, version, vuln_id} dicts.
    """
    text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    vulns: list[dict] = []

    # pip-audit output format: "Name Version ID Fix Versions"
    # Vulnerable lines usually have a CVE/PYSEC ID pattern
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Match: package version PYSEC-YYYY-NNNN or CVE-YYYY-NNNNN
        match = re.match(
            r"^(\S+)\s+(\S+)\s+(CVE-\d{4}-\d{4,}|PYSEC-\d{4}-\d{3,}|GHSA-[a-z0-9-]+)",
            line,
        )
        if match:
            vulns.append(
                {
                    "package": match.group(1),
                    "version": match.group(2),
                    "vuln_id": match.group(3),
                }
            )

    return vulns


def check_exemptions(vulns: list[dict], exemptions_data: dict) -> tuple[bool, list[str]]:
    """Check if all vulnerabilities are covered by exemptions.

    :param vulns: Parsed vulnerability list.
    :param exemptions_data: Exemptions JSON dict.
    :returns: (all_covered, uncovered_descriptions).
    """
    exempted_ids: set[str] = set()
    expired_ids: set[str] = set()
    today = date.today()

    for ex in exemptions_data.get("exemptions", []):
        vuln_id = ex.get("cve", "")
        if not vuln_id:
            continue
        expires = ex.get("expires", "")
        if expires:
            try:
                expiry_date = datetime.strptime(expires, "%Y-%m-%d").date()
                if today > expiry_date:
                    expired_ids.add(vuln_id)
                    continue
            except ValueError:
                pass
        exempted_ids.add(vuln_id)

    uncovered: list[str] = []
    for v in vulns:
        vid = v["vuln_id"]
        if vid in expired_ids:
            uncovered.append(f"{v['package']}@{v['version']}: {vid} (exemption EXPIRED)")
        elif vid not in exempted_ids:
            uncovered.append(f"{v['package']}@{v['version']}: {vid} (no exemption)")

    return len(uncovered) == 0, uncovered


def main() -> int:
    """CLI entry point.

    :returns: Exit code 0 or 1.
    """
    if len(sys.argv) < 2:
        print("Usage: python scripts/check_pip_audit_exemptions.py <pip-audit.log>", file=sys.stderr)
        return 1

    log_path = sys.argv[1]
    if not Path(log_path).exists():
        print(f"Log file not found: {log_path}", file=sys.stderr)
        return 1

    try:
        exemptions = load_exemptions()
        vulns = parse_vulnerabilities(log_path)

        if not vulns:
            print("pip-audit: no vulnerabilities found")
            return 0

        all_covered, uncovered = check_exemptions(vulns, exemptions)

        print(f"pip-audit: {len(vulns)} vulnerability(s) found")
        print(f"  Exempted: {len(vulns) - len(uncovered)}")
        print(f"  Uncovered: {len(uncovered)}")

        for item in uncovered[:20]:
            print(f"    {item}")

        return 0 if all_covered else 1

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
