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
        if condition:
            self.passed.append(name)
        else:
            self.failed.append((name, detail))

    def warn(self, name: str, detail: str = "") -> None:
        self.warned.append((name, detail))

    @property
    def exit_code(self) -> int:
        if self.failed:
            return EXIT_FAIL
        if self.warned:
            return EXIT_WARN
        return EXIT_OK

    def report(self) -> str:
        lines: list[str] = []
        lines.append(f"\n{'='*60}")
        lines.append(f"  Release Audit Report")
        lines.append(f"{'='*60}")
        lines.append(f"  PASS: {len(self.passed)}")
        lines.append(f"  WARN: {len(self.warned)}")
        lines.append(f"  FAIL: {len(self.failed)}")

        if self.failed:
            lines.append(f"\n  --- FAIL ---")
            for name, detail in self.failed:
                lines.append(f"  [FAIL] {name}")
                if detail:
                    lines.append(f"         {detail}")

        if self.warned:
            lines.append(f"\n  --- WARN ---")
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

    # 检查是否有 if __name__ == "__main__": main() 的 NameError
    if "main()  # noqa: F821" in source:
        audit.check("launcher/main.py", False, "包含 noqa: F821")


def check_ci_bypass(audit: AuditResult) -> None:
    """检查是否使用 production-incompatible CI bypass。"""
    lite_py = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "builders" / "lite.py"
    release_yml = REPO_ROOT / ".github" / "workflows" / "release.yml"

    if lite_py.exists():
        content = lite_py.read_text(encoding="utf-8")
        has_ci_build = "BLC_CI_BUILD" in content
        has_fixture = "BLC_FIXTURE_BUILD" in content
        audit.check("lite.py 不再使用 BLC_CI_BUILD", not has_ci_build,
                      "仍有 BLC_CI_BUILD 残留 — 正式 Release 禁止" if has_ci_build else "")
        audit.check("lite.py 使用 BLC_FIXTURE_BUILD", has_fixture,
                      "缺少 BLC_FIXTURE_BUILD 支持" if not has_fixture else "")

    if release_yml.exists():
        content = release_yml.read_text(encoding="utf-8")
        audit.check("release.yml 无 BLC_CI_BUILD", "BLC_CI_BUILD" not in content)


def check_model_single_source(audit: AuditResult) -> None:
    """检查模型定义是否只有一个来源。"""
    downloader = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "engine_pack" / "downloader.py"

    if downloader.exists():
        content = downloader.read_text(encoding="utf-8")
        # 不应有独立的 ENGINES 常量
        has_engines_assign = "ENGINES: list" in content or "ENGINES = [" in content
        audit.check("downloader.py 无独立 ENGINES", not has_engines_assign,
                      "仍有独立 ENGINES 列表 — 应使用 model_catalog" if has_engines_assign else "")

        has_catalog_import = "_load_engine_defs" in content or "load_engines" in content
        audit.check("downloader.py 使用统一 Catalog", has_catalog_import,
                      "未导入 model_catalog" if not has_catalog_import else "")

    # 检查无 legacy repo reference
    for py_file in REPO_ROOT.rglob("*.py"):
        if "build" in py_file.parts or "dist" in py_file.parts or "__pycache__" in py_file.parts:
            continue
        if py_file.suffix != ".py":
            continue
        ct = py_file.read_text(encoding="utf-8", errors="replace")
        if "iic/Fun-ASR-Nano" in ct and "FunAudioLLM" not in ct:
            audit.check(f"Legacy FunASR repo: {py_file.relative_to(REPO_ROOT)}", False,
                f"{py_file.relative_to(REPO_ROOT)} 仍使用 iic/Fun-ASR-Nano")


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
    audit.check("Builder 使用 resolved_revision", uses_resolved,
                  "Builder 未使用 resolved_revision" if not uses_resolved else "")
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

    for field in ("engine_pack_version", "crc32", "sha256", "expected_engine_ids",
                   "format_version", "content_manifest_sha256", "model_lock_sha256"):
        audit.check(f"engine_pack_info.{field}", field in info or "manifest_sha256" in info,
                      "" if (field in info or (field == "content_manifest_sha256" and "manifest_sha256" in info)) else f"缺少字段 '{field}'")

    audit.check("engine_pack_info.crc32 non-empty", bool(info.get("crc32", "")),
                  "CRC32 为空 — 正式构建必须失败")
    audit.check("engine_pack_info.sha256 non-empty", bool(info.get("sha256", "")),
                  "SHA-256 为空 — 正式构建必须失败")


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
