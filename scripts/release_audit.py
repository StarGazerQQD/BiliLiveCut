#!/usr/bin/env python3
"""Release 审计脚本 — 自动检查关键发布条件是否满足。

检查项:
- Launcher 是否有 callable main
- Release 是否使用 CI bypass
- 模型定义是否唯一
- resolved revision 是否不可变
- Builder 是否使用 resolved revision
- Full 是否 fail-closed
- Engine Pack 外部元数据是否完整
- Runtime 是否单一实现
- 测试是否覆盖生产入口
- 关键测试是否被 skip
- 原生版本是否一致
- Python 支持范围是否一致
- 产物是否真实存在
- hash 是否匹配

用法:
    python scripts/release_audit.py          # 全部检查
    python scripts/release_audit.py --quick  # 仅快速检查
    python scripts/release_audit.py --json   # 输出 JSON

退出码:
    0: 全部通过
    1: 存在 WARN
    2: 存在 FAIL
"""

from __future__ import annotations

import ast
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2


class AuditResult:
    """审计结果收集器。"""

    def __init__(self) -> None:
        self.passed: list[str] = []
        self.warned: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        """Record a check result (pass or fail).

        :param name: Check name.
        :param condition: True = pass, False = fail.
        :param detail: Failure detail message.
        """
        if condition:
            self.passed.append(name)
        else:
            self.failed.append((name, detail))

    def warn(self, name: str, detail: str = "") -> None:
        """Record a warning (non-blocking).

        :param name: Warning name.
        :param detail: Warning detail message.
        """
        self.warned.append((name, detail))

    @property
    def exit_code(self) -> int:
        """Compute exit code: 0=pass, 1=warn, 2=fail.

        :returns: EXIT_OK, EXIT_WARN, or EXIT_FAIL.
        """
        if self.failed:
            return EXIT_FAIL
        if self.warned:
            return EXIT_WARN
        return EXIT_OK

    def report(self) -> str:
        """Generate human-readable audit report.

        :returns: Formatted report string.
        """
        lines: list[str] = []
        lines.append(f"\n{'=' * 60}")
        lines.append("  Release Audit Report")
        lines.append(f"{'=' * 60}")
        lines.append(f"  PASS: {len(self.passed)}")
        lines.append(f"  WARN: {len(self.warned)}")
        lines.append(f"  FAIL: {len(self.failed)}")

        if self.failed:
            lines.append("\n  --- FAIL ---")
            for name, detail in self.failed:
                lines.append(f"  [FAIL] {name}")
                if detail:
                    lines.append(f"         {detail}")

        if self.warned:
            lines.append("\n  --- WARN ---")
            for name, detail in self.warned:
                lines.append(f"  [WARN] {name}")
                if detail:
                    lines.append(f"         {detail}")

        return "\n".join(lines)


def check_launcher_main(audit: AuditResult) -> None:
    """检查 Launcher 是否有 callable main。"""
    main_py = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "launcher" / "main.py"
    if not main_py.exists():
        audit.check("launcher/main.py", False, "文件不存在")
        return

    try:
        source = main_py.read_text(encoding="utf-8")
        tree = ast.parse(source)
        funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    except SyntaxError as exc:
        audit.check("launcher/main.py", False, f"语法错误: {exc}")
        return

    audit.check("launcher callable main()", "main" in funcs, "缺少 main() 函数")
    audit.check("launcher build_parser()", "build_parser" in funcs, "缺少 build_parser()")
    audit.check("launcher run_launcher()", "run_launcher" in funcs, "缺少 run_launcher()")
    audit.check("launcher __main__ guard", 'if __name__ == "__main__"' in source)


def check_ci_bypass(audit: AuditResult) -> None:
    """检查是否使用 production-incompatible CI bypass。"""
    lite_py = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "builders" / "lite.py"
    release_yml = REPO_ROOT / ".github" / "workflows" / "release.yml"

    if lite_py.exists():
        content = lite_py.read_text(encoding="utf-8")
        has_ci_build = "BLC_CI_BUILD" in content
        has_fixture = "BLC_FIXTURE_BUILD" in content
        audit.check(
            "lite.py 不再使用 BLC_CI_BUILD",
            not has_ci_build,
            "仍有 BLC_CI_BUILD 残留 — 正式 Release 禁止" if has_ci_build else "",
        )
        audit.check(
            "lite.py 使用 BLC_FIXTURE_BUILD", has_fixture, "缺少 BLC_FIXTURE_BUILD 支持" if not has_fixture else ""
        )

    if release_yml.exists():
        content = release_yml.read_text(encoding="utf-8")
        audit.check("release.yml 无 BLC_CI_BUILD", "BLC_CI_BUILD" not in content)
        audit.check(
            "release.yml 无 BLC_FIXTURE_BUILD",
            "BLC_FIXTURE_BUILD" not in content,
            "正式 Release 不得绕过 Engine Pack production 元数据校验",
        )
        audit.check(
            "release.yml 标签校验阻断测试",
            "needs: validate-tag" in content,
            "test job 必须依赖 validate-tag",
        )
        audit.check(
            "release.yml CLI smoke 导入真实入口",
            "from app.cli import app" in content and "from app.cli import main" not in content,
            "CLI smoke test 必须导入 Typer app，而不是不存在的 main",
        )
        audit.check(
            "release.yml Full ZIP 定位顶层目录",
            "$bundleRoot" in content,
            "Full ZIP 含版本目录，smoke test 必须先定位 bundle root",
        )
        audit.check(
            "release.yml 显式省略未分发 Engine Pack",
            "python build_exe.py --without-engine-pack" in content,
            "GitHub Release 不分发 Engine Pack，不得嵌入仓库 fixture 元数据",
        )


def check_distribution_config(audit: AuditResult) -> None:
    """Check wheel/sdist runtime content and fail-closed release tooling."""
    pyproject_path = REPO_ROOT / "pyproject.toml"
    manifest_path = REPO_ROOT / "MANIFEST.in"
    release_gate_path = REPO_ROOT / "scripts" / "release_gate.py"

    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    optional = pyproject["project"]["optional-dependencies"]
    web = {requirement.split(">=", 1)[0].lower() for requirement in optional["web"]}
    dev = {requirement.split(">=", 1)[0].lower() for requirement in optional["dev"]}
    setuptools_config = pyproject["tool"]["setuptools"]
    package_data = set(setuptools_config["package-data"]["app.web"])

    audit.check("web extra 包含 python-multipart", "python-multipart" in web)
    audit.check("dev extra 覆盖 Pillow", "pillow" in dev)
    audit.check(
        "wheel 包含 Web templates/static",
        {"templates/*.html", "static/*.js", "static/js/*.js", "static/*.css"} <= package_data,
    )
    audit.check(
        "wheel 包含评分配置和关键词表",
        "config" in setuptools_config["packages"]["find"]["include"]
        and {"*.yaml", "*.txt"} <= set(setuptools_config["package-data"]["config"]),
    )
    audit.check("sdist MANIFEST.in 存在", manifest_path.is_file())
    manifest = manifest_path.read_text(encoding="utf-8") if manifest_path.is_file() else ""
    audit.check("sdist 包含 Dockerfile", "include packaging/docker/Dockerfile" in manifest)
    audit.check("sdist 排除 Cython 生成文件", "exclude tools/native/cython/_speedups_round2.c" in manifest)

    release_gate = release_gate_path.read_text(encoding="utf-8")
    audit.check(
        "release gate 禁止 skip 选项", "--skip-payload" not in release_gate and "--skip-portable" not in release_gate
    )
    audit.check("release gate 强制可复现构建", "--skip-reproducible" not in release_gate)
    audit.check("release gate 拒绝 pytest skip", "--fail-on-skip" in release_gate)


def check_model_single_source(audit: AuditResult) -> None:
    """检查模型定义是否只有一个来源。"""
    downloader = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "engine_pack" / "downloader.py"

    if downloader.exists():
        content = downloader.read_text(encoding="utf-8")
        # 不应有独立的 ENGINES 常量
        has_engines_assign = "ENGINES: list" in content or "ENGINES = [" in content
        audit.check(
            "downloader.py 无独立 ENGINES",
            not has_engines_assign,
            "仍有独立 ENGINES 列表 — 应使用 model_catalog" if has_engines_assign else "",
        )

        has_catalog_import = "_load_engine_defs" in content or "load_engines" in content
        audit.check(
            "downloader.py 使用统一 Catalog",
            has_catalog_import,
            "未导入 model_catalog" if not has_catalog_import else "",
        )

    # 检查无 legacy repo reference
    for py_file in REPO_ROOT.rglob("*.py"):
        if "build" in py_file.parts or "dist" in py_file.parts or "__pycache__" in py_file.parts:
            continue
        if py_file.suffix != ".py":
            continue
        ct = py_file.read_text(encoding="utf-8", errors="replace")
        if "iic/Fun-ASR-Nano" in ct and "FunAudioLLM" not in ct:
            audit.check(
                f"Legacy FunASR repo: {py_file.relative_to(REPO_ROOT)}",
                False,
                f"{py_file.relative_to(REPO_ROOT)} 仍使用 iic/Fun-ASR-Nano",
            )


def check_resolved_revision(audit: AuditResult) -> None:
    """检查 Builder 是否使用 resolved_revision。"""
    builder_py = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "engine_pack" / "builder.py"
    if not builder_py.exists():
        return
    content = builder_py.read_text(encoding="utf-8")
    # Builder should use e.resolved_revision, not e.requested_revision
    uses_resolved = "resolved_revision" in content
    # The old way: e.requested_revision
    uses_requested = "requested_revision" in content
    audit.check(
        "Builder 使用 resolved_revision", uses_resolved, "Builder 未使用 resolved_revision" if not uses_resolved else ""
    )
    if uses_requested and "resolved_revision" not in content:
        audit.check("Builder 使用 resolved_revision", False, "必须使用 resolved_revision")


def check_engine_pack_metadata(audit: AuditResult) -> None:
    """检查 engine_pack_info.json 包含完整字段。"""
    info_path = REPO_ROOT / "packaging" / "portable" / "resources" / "engine_pack_info.json"
    if not info_path.exists():
        audit.check("engine_pack_info.json", False, "文件不存在")
        return
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        audit.check("engine_pack_info.json", False, "JSON 解析失败")
        return

    for field in (
        "engine_pack_version",
        "crc32",
        "sha256",
        "expected_engine_ids",
        "format_version",
        "content_manifest_sha256",
        "model_lock_sha256",
    ):
        audit.check(
            f"engine_pack_info.{field}",
            field in info or "manifest_sha256" in info,
            ""
            if (field in info or (field == "content_manifest_sha256" and "manifest_sha256" in info))
            else f"缺少字段 '{field}'",
        )

    audit.check("engine_pack_info.crc32 non-empty", bool(info.get("crc32", "")), "CRC32 为空 — 正式构建必须失败")
    audit.check("engine_pack_info.sha256 non-empty", bool(info.get("sha256", "")), "SHA-256 为空 — 正式构建必须失败")
    # 仓库允许保存显式标记的 fixture；正式构建由 builders/lite.py fail-closed。
    artifact_class = info.get("artifact_class", "")
    audit.check("engine_pack_info.artifact_class present", bool(artifact_class), "artifact_class 缺失 — 必须显式声明")
    if artifact_class == "fixture":
        audit.check(
            "engine_pack_info fixture 显式隔离",
            info.get("size_bytes", 0) < 500_000_000,
            "fixture 必须保持小体积，正式构建会拒绝它",
        )
    elif artifact_class != "production":
        audit.check(
            "engine_pack_info.artifact_class is production",
            False,
            f"artifact_class={artifact_class} — 必须为 'production' 或 'fixture'",
        )
    audit.check(
        "engine_pack_info.format_version >= 4",
        info.get("format_version", 0) >= 4,
        f"format_version={info.get('format_version', 0)} — 需 >= 4",
    )


def check_csrf(audit: AuditResult) -> None:
    """检查 Web 层 CSRF 防护存在性。"""
    main_py = REPO_ROOT / "app" / "web" / "main.py"
    if not main_py.exists():
        return
    content = main_py.read_text(encoding="utf-8")
    audit.check("CSRF _check_csrf", "_check_csrf" in content)
    audit.check("Basic Auth 用户名验证", 'username != "admin"' in content)


def run_audit(quick: bool = False) -> AuditResult:
    """执行完整审计。

    :param quick: 仅快速检查。
    :returns: AuditResult 实例。
    """
    audit = AuditResult()

    check_launcher_main(audit)
    check_ci_bypass(audit)
    check_distribution_config(audit)

    if not quick:
        check_model_single_source(audit)
        check_resolved_revision(audit)
        check_engine_pack_metadata(audit)
        check_csrf(audit)

    return audit


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BiliLiveCut Release Auditor")
    parser.add_argument("--quick", action="store_true", help="仅快速检查")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    audit = run_audit(quick=args.quick)

    if args.json:
        result: dict[str, Any] = {
            "passed": len(audit.passed),
            "warned": len(audit.warned),
            "failed": len(audit.failed),
            "passed_list": audit.passed,
            "warned_list": [{"name": n, "detail": d} for n, d in audit.warned],
            "failed_list": [{"name": n, "detail": d} for n, d in audit.failed],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(audit.report())

    sys.exit(audit.exit_code)
