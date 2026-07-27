#!/usr/bin/env python3
"""Validate a pip-audit JSON report against scoped, expiring exemptions."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXEMPTIONS_PATH = REPO_ROOT / "scripts" / "pip-audit-exemptions.json"
CANONICAL_NAME_PATTERN = re.compile(r"[-_.]+")
REQUIRED_EXEMPTION_FIELDS = ("cve", "package", "reason", "owner", "expires")


class AuditReportError(ValueError):
    """Raised when pip-audit output is missing or structurally invalid."""


@dataclass(frozen=True)
class Vulnerability:
    """One vulnerability attached to one resolved package version."""

    package: str
    version: str
    vuln_id: str
    aliases: tuple[str, ...]

    @property
    def identifiers(self) -> frozenset[str]:
        """Return the primary identifier and all aliases."""
        return frozenset((self.vuln_id, *self.aliases))


@dataclass(frozen=True)
class AuditReport:
    """Normalized dependency and vulnerability data from pip-audit."""

    dependencies: frozenset[tuple[str, str]]
    vulnerabilities: tuple[Vulnerability, ...]


def canonicalize_name(name: str) -> str:
    """Apply the package-name normalization used by Python packaging tools."""
    return CANONICAL_NAME_PATTERN.sub("-", name).lower()


def load_exemptions(path: Path = EXEMPTIONS_PATH) -> dict[str, object]:
    """Load and validate the machine-readable exemptions file."""
    if not path.is_file():
        raise FileNotFoundError(f"Exemptions file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format_version") != 1:
        raise AuditReportError("Exemptions file must be an object with format_version=1")
    exemptions = data.get("exemptions")
    if not isinstance(exemptions, list):
        raise AuditReportError("Exemptions file must contain an exemptions list")

    seen: set[tuple[str, str]] = set()
    for index, exemption in enumerate(exemptions, start=1):
        if not isinstance(exemption, dict):
            raise AuditReportError(f"Exemption #{index} must be an object")
        missing = [field for field in REQUIRED_EXEMPTION_FIELDS if not exemption.get(field)]
        if missing:
            raise AuditReportError(f"Exemption #{index} missing required fields: {', '.join(missing)}")
        try:
            datetime.strptime(str(exemption["expires"]), "%Y-%m-%d")
        except ValueError as exc:
            raise AuditReportError(f"Exemption #{index} has invalid expires date") from exc
        key = (canonicalize_name(str(exemption["package"])), str(exemption["cve"]))
        if key in seen:
            raise AuditReportError(f"Duplicate exemption for {key[0]}: {key[1]}")
        seen.add(key)
    return data


def parse_audit_report(text: str) -> AuditReport:
    """Parse pip-audit JSON and reject incomplete or ambiguous output."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuditReportError("pip-audit did not emit valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("dependencies"), list):
        raise AuditReportError("pip-audit JSON must contain a dependencies list")

    dependencies: set[tuple[str, str]] = set()
    vulnerabilities: dict[tuple[str, str, frozenset[str]], Vulnerability] = {}
    for index, dependency in enumerate(payload["dependencies"], start=1):
        if not isinstance(dependency, dict):
            raise AuditReportError(f"Dependency #{index} must be an object")
        name = dependency.get("name")
        version = dependency.get("version")
        vuln_items = dependency.get("vulns")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise AuditReportError(f"Dependency #{index} is missing name or version")
        if not isinstance(vuln_items, list):
            raise AuditReportError(f"Dependency {name} is missing its vulns list")

        package = canonicalize_name(name)
        package_version = (package, version)
        if package_version in dependencies:
            raise AuditReportError(f"Duplicate dependency in pip-audit JSON: {package}=={version}")
        dependencies.add(package_version)

        for vuln_index, vuln in enumerate(vuln_items, start=1):
            if not isinstance(vuln, dict) or not isinstance(vuln.get("id"), str) or not vuln["id"]:
                raise AuditReportError(f"Vulnerability #{vuln_index} for {package} has no identifier")
            aliases_raw = vuln.get("aliases", [])
            if not isinstance(aliases_raw, list) or not all(isinstance(alias, str) for alias in aliases_raw):
                raise AuditReportError(f"Vulnerability {vuln['id']} for {package} has invalid aliases")
            aliases = tuple(sorted(set(aliases_raw)))
            item = Vulnerability(package, version, str(vuln["id"]), aliases)
            key = (package, version, item.identifiers)
            vulnerabilities.setdefault(key, item)

    return AuditReport(frozenset(dependencies), tuple(vulnerabilities.values()))


def parse_vulnerabilities(report_path: str | Path) -> list[Vulnerability]:
    """Load a pip-audit JSON report and return its vulnerabilities."""
    text = Path(report_path).read_text(encoding="utf-8", errors="strict")
    return list(parse_audit_report(text).vulnerabilities)


def check_exemptions(
    vulnerabilities: Iterable[Vulnerability], exemptions_data: dict[str, object]
) -> tuple[bool, list[str]]:
    """Return whether every vulnerability has a package-scoped active exemption."""
    exemptions_raw = exemptions_data.get("exemptions", [])
    if not isinstance(exemptions_raw, list):
        raise AuditReportError("Exemptions data must contain an exemptions list")

    today = date.today()
    uncovered: list[str] = []
    for vulnerability in vulnerabilities:
        matching: list[dict[str, object]] = []
        for exemption in exemptions_raw:
            if not isinstance(exemption, dict):
                raise AuditReportError("Each exemption must be an object")
            if canonicalize_name(str(exemption.get("package", ""))) != vulnerability.package:
                continue
            if str(exemption.get("cve", "")) in vulnerability.identifiers:
                matching.append(exemption)

        active = False
        expired = False
        for exemption in matching:
            expires = datetime.strptime(str(exemption["expires"]), "%Y-%m-%d").date()
            if expires >= today:
                active = True
            else:
                expired = True
        if not active:
            reason = "exemption EXPIRED" if expired else "no package-scoped exemption"
            identifiers = ", ".join(sorted(vulnerability.identifiers))
            uncovered.append(f"{vulnerability.package}@{vulnerability.version}: {identifiers} ({reason})")

    return not uncovered, uncovered


def main(argv: list[str] | None = None) -> int:
    """Validate one JSON report supplied on the command line."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Usage: python scripts/check_pip_audit_exemptions.py <pip-audit.json>", file=sys.stderr)
        return 2

    try:
        exemptions = load_exemptions()
        report = parse_audit_report(Path(args[0]).read_text(encoding="utf-8", errors="strict"))
        covered, uncovered = check_exemptions(report.vulnerabilities, exemptions)
    except (AuditReportError, FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"pip-audit report validation failed: {exc}", file=sys.stderr)
        return 2

    print(f"pip-audit: {len(report.dependencies)} dependencies, {len(report.vulnerabilities)} vulnerabilities")
    for item in uncovered:
        print(f"  {item}")
    return 0 if covered else 1


if __name__ == "__main__":
    raise SystemExit(main())
