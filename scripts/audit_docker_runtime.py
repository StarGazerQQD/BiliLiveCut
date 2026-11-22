"""审计实际 Docker 镜像的 Python 依赖，拒绝漏扫、未知版本及审计服务失败。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from runpy import run_path

from packaging.version import Version

if __package__:
    from scripts.check_pip_audit_exemptions import AuditReportError, canonicalize_name, parse_audit_report
else:
    from check_pip_audit_exemptions import AuditReportError, canonicalize_name, parse_audit_report

ROOT = Path(__file__).resolve().parents[1]
PROJECT_VERSION = Version(run_path(str(ROOT / "packaging/portable/config/version_loader.py"))["RELEASE_VERSION"])
APPLICATION_PACKAGES = frozenset({"bili-live-cut", "torch", "torchaudio"})
SYSTEM_PACKAGES: frozenset[str] = frozenset()
INVENTORY_COMMAND = (
    "import importlib.metadata as m, json; "
    "print(json.dumps([{'name': d.metadata['Name'], 'version': d.version} for d in m.distributions()]))"
)


def dependencies_from_inventory(
    text: str, *, required_packages: frozenset[str] = APPLICATION_PACKAGES
) -> frozenset[tuple[str, str]]:
    """校验镜像清单，只对官方 Torch CPU 构建去掉本地版本后缀以查询公共漏洞记录。"""
    entries = json.loads(text)
    if not isinstance(entries, list) or (not entries and required_packages):
        raise AuditReportError("镜像依赖清单必须是非空列表")
    dependencies: set[tuple[str, str]] = set()
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise AuditReportError("镜像依赖记录必须是对象")
        name, raw_version = entry.get("name"), entry.get("version")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name):
            raise AuditReportError("镜像依赖名称无效")
        if not isinstance(raw_version, str) or not raw_version:
            raise AuditReportError(f"{name} 缺少版本")
        name = canonicalize_name(name)
        if name in seen:
            raise AuditReportError(f"镜像包含重复依赖：{name}")
        seen.add(name)
        version = Version(raw_version)
        if name == "bili-live-cut":
            if version != PROJECT_VERSION:
                raise AuditReportError("镜像应用版本与仓库版本不一致")
            continue  # 自有项目通过代码审计和测试验收，不伪装成 PyPI 第三方包。
        if version.local and not (name in {"torch", "torchaudio"} and version.local == "cpu"):
            raise AuditReportError(f"无法映射到公共漏洞记录的本地版本：{name}=={version}")
        dependencies.add((name, version.public))
    if not required_packages <= seen:
        raise AuditReportError(f"镜像缺少必需依赖：{sorted(required_packages - seen)}")
    return frozenset(dependencies)


def audit_inventory(text: str, output_dir: Path, *, required_packages: frozenset[str] = APPLICATION_PACKAGES) -> bool:
    """保留实际清单与审计报告；检查所有依赖均已扫描且没有已知漏洞。"""
    dependencies = dependencies_from_inventory(text, required_packages=required_packages)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "inventory.json").write_text(text, encoding="utf-8")
    if not dependencies:
        (output_dir / "inventory-check.json").write_text(
            json.dumps({"third_party_packages": 0, "vulnerability_query_executed": False}), encoding="utf-8"
        )
        print("系统 Python 仅含标准库，无第三方依赖可查询。")
        return True
    requirements = output_dir / "requirements.txt"
    requirements.write_text("".join(f"{name}=={version}\n" for name, version in sorted(dependencies)), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip_audit",
            "--disable-pip",
            "--no-deps",
            "--progress-spinner",
            "off",
            "-r",
            str(requirements),
            "-f",
            "json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    (output_dir / "pip-audit.json").write_text(result.stdout, encoding="utf-8")
    report = parse_audit_report(result.stdout)
    if report.dependencies != dependencies:
        raise AuditReportError("审计报告与镜像依赖不一致，存在遗漏或版本变化")
    if result.returncode != 0 or report.vulnerabilities:
        for vulnerability in report.vulnerabilities:
            print(f"{vulnerability.package}=={vulnerability.version}: {vulnerability.identifiers}", file=sys.stderr)
        raise AuditReportError(f"镜像依赖审计未通过：exit={result.returncode}")
    print(f"Docker Python 依赖审计通过：{len(dependencies)} 个第三方依赖，无已知漏洞。")
    return True


def main(argv: list[str] | None = None) -> int:
    """在无网络、只读容器中提取安装清单，在宿主独立工具环境执行漏洞查询。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="bili-live-cut:docker-test")
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--output-dir", type=Path, default=Path(".local/docker-audit"))
    args = parser.parse_args(argv)
    try:
        for scope, python, required in (
            ("application", "/app/.venv/bin/python", APPLICATION_PACKAGES),
            ("system", "/usr/local/bin/python", SYSTEM_PACKAGES),
        ):
            result = subprocess.run(
                [
                    args.docker,
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--entrypoint",
                    python,
                    args.image,
                    "-I",
                    "-c",
                    INVENTORY_COMMAND
                    if scope == "application"
                    else (
                        "import importlib.util, ensurepip; from pathlib import Path; "
                        "assert importlib.util.find_spec('pip') is None, 'runtime contains pip'; "
                        "assert not list((Path(ensurepip.__file__).parent / '_bundled').glob('*.whl')), "
                        "'runtime contains bootstrap wheels'; " + INVENTORY_COMMAND
                    ),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=180,
            )
            print(f"审计镜像 Python 环境：{scope}")
            audit_inventory(result.stdout, args.output_dir / scope, required_packages=required)
    except (OSError, ValueError, subprocess.SubprocessError, AuditReportError) as exc:
        print(f"Docker 依赖审计失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
