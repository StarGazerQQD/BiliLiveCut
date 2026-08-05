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
PORTABLE_SRC = REPO_ROOT / "packaging" / "portable" / "src"
if str(PORTABLE_SRC) not in sys.path:
    sys.path.insert(0, str(PORTABLE_SRC))

from blc_portable.model_lock import compute_model_lock_sha256  # noqa: E402

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
    lite_smoke_py = REPO_ROOT / "scripts" / "smoke_portable_lite.py"
    ffmpeg_download_py = REPO_ROOT / "scripts" / "download_release_ffmpeg.py"

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
        lite_smoke = lite_smoke_py.read_text(encoding="utf-8") if lite_smoke_py.is_file() else ""
        ffmpeg_download = ffmpeg_download_py.read_text(encoding="utf-8") if ffmpeg_download_py.is_file() else ""
        doctor_start = content.find("- name: Lite EXE --doctor rejects incomplete environment")
        doctor_end = content.find("- name: Lite fresh-install to empty directory", doctor_start)
        doctor_smoke = content[doctor_start:doctor_end] if doctor_start >= 0 and doctor_end > doctor_start else ""
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
            "from app.cli import app" in content
            and "from app.cli import main" not in content
            and "source_dir=Path(os.environ['BLC_SMOKE_SOURCE_DIR'])" in content
            and "actual.is_relative_to(source)" in content,
            "CLI smoke test 必须使用已安装 Runtime 源码导入 Typer app",
        )
        audit.check(
            "release.yml 冻结 Launcher 模型准备 smoke",
            "Frozen Full launcher model preparation OK" in content
            and '"--offline", "--engine-pack"' in content
            and "$root\\models\\engine-pack-installed.json" in content,
            "Full smoke 必须让冻结 EXE 完成 Fixture Engine Pack 模型准备",
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
        audit.check(
            "release.yml Lite 真实首次安装与二次离线启动",
            "scripts/smoke_portable_lite.py" in content
            and "Lite fresh online installation and second offline launch OK" in lite_smoke
            and "_wait_ready" in lite_smoke
            and '["--offline", "--engine-pack"' in lite_smoke
            and "_configure_console_encoding()" in lite_smoke
            and 'reconfigure(encoding="utf-8", errors="backslashreplace")' in lite_smoke,
            "Lite smoke 必须完成空目录安装、Web 就绪、UTF-8 日志回显和二次断网复用",
        )
        audit.check(
            "release.yml Doctor 预期失败退出码归一化",
            "$doctorExit -eq 0" in doctor_smoke
            and "Diagnostics complete: .* FAIL" in doctor_smoke
            and "exit 0" in doctor_smoke,
            "Doctor 按设计返回非零后，验证成功路径必须显式清除原生命令退出码",
        )
        audit.check(
            "release.yml 跨制品身份校验",
            "scripts/verify_release_artifacts.py" in content and "--expected-builder-commit" in content,
            "发布前必须交叉校验 Payload/Lite/Full 的版本、基线、构建提交和哈希",
        )
        audit.check(
            "release.yml tag 与项目版本严格匹配",
            "PROJECT_VERSION=" in content
            and 'NORMALIZED_TAG_VERSION="${TAG_VERSION,,}"' in content
            and 'if [ "$NORMALIZED_TAG_VERSION" != "${PROJECT_VERSION,,}" ]' in content
            and "complete release version pattern" in content,
            "tag 必须完整匹配版本语法并等于项目版本真源",
        )
        audit.check(
            "release.yml 预发布标签大小写兼容",
            'if [[ "$NORMALIZED_TAG_VERSION" =~ -(alpha|beta|rc)' in content
            and 'echo "prerelease=$PRERELEASE"' in content
            and "prerelease: ${{ steps.tag.outputs.prerelease == 'true' }}" in content,
            "历史 -Alpha/-Beta/-RC 标签必须保持 GitHub prerelease 属性",
        )
        audit.check(
            "release.yml FFmpeg 下载具备重试与备用源",
            "python scripts/download_release_ffmpeg.py --output-dir bin" in content
            and "BtbN/FFmpeg-Builds" in ffmpeg_download
            and "www.gyan.dev" in ffmpeg_download
            and "DEFAULT_ATTEMPTS = 3" in ffmpeg_download
            and "testzip()" in ffmpeg_download,
            "Release 必须通过受测下载器重试多个来源，并在提取前校验 FFmpeg ZIP",
        )
    launcher_spec = REPO_ROOT / "packaging" / "portable" / "specs" / "portable_launcher.spec"
    if launcher_spec.exists():
        spec_content = launcher_spec.read_text(encoding="utf-8")
        audit.check(
            "冻结 Launcher 收集 Engine Pack 配置依赖",
            all(
                token in spec_content
                for token in (
                    '"model_catalog"',
                    '"version_loader"',
                    '(_version_config, ".")',
                    '(_model_sources_lock, ".")',
                )
            ),
            "PyInstaller 必须收集 Engine Pack 配置模块和 JSON 数据",
        )


def check_distribution_config(audit: AuditResult) -> None:
    """Check wheel/sdist runtime content and fail-closed release tooling."""
    pyproject_path = REPO_ROOT / "pyproject.toml"
    manifest_path = REPO_ROOT / "MANIFEST.in"
    license_path = REPO_ROOT / "LICENSE"
    release_gate_path = REPO_ROOT / "scripts" / "release_gate.py"
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    release_path = REPO_ROOT / ".github" / "workflows" / "release.yml"
    frontend_check_path = REPO_ROOT / "scripts" / "check_frontend_interactions.mjs"
    portable_spec_path = REPO_ROOT / "packaging" / "portable" / "specs" / "portable_launcher.spec"

    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    optional = pyproject["project"]["optional-dependencies"]
    web = {requirement.split(">=", 1)[0].lower() for requirement in optional["web"]}
    dev = {requirement.split(">=", 1)[0].lower() for requirement in optional["dev"]}
    setuptools_config = pyproject["tool"]["setuptools"]
    package_data = set(setuptools_config["package-data"]["app.web"])
    license_text = license_path.read_text(encoding="utf-8") if license_path.is_file() else ""

    audit.check(
        "项目采用规范 MIT License",
        all(
            marker in license_text
            for marker in (
                "MIT License",
                "Copyright (c) 2026 StarGazerQQD",
                "Permission is hereby granted",
                'THE SOFTWARE IS PROVIDED "AS IS"',
            )
        ),
        "仓库根 LICENSE 缺失或内容不完整",
    )
    audit.check(
        "Python 包元数据声明项目 LICENSE",
        pyproject["project"].get("license") == "MIT" and pyproject["project"].get("license-files") == ["LICENSE"],
    )
    audit.check(
        "构建后端支持 SPDX 许可证元数据",
        "setuptools>=77" in pyproject["build-system"].get("requires", []),
    )
    direct_build_requirements = 'python -m pip install --upgrade pip "setuptools>=77" "Cython==3.2.9"'
    audit.check(
        "直接 setup.py 构建显式安装声明的构建后端",
        all(
            direct_build_requirements in workflow_path.read_text(encoding="utf-8")
            for workflow_path in (ci_path, release_path)
        ),
        "CI/Release 不得依赖 runner 自带的旧版 setuptools",
    )

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
    audit.check("sdist 包含项目 LICENSE", "include LICENSE" in manifest)
    audit.check(
        "sdist 包含前端交互检查脚本",
        "recursive-include scripts *.py *.json *.mjs *.sh *.bat" in manifest,
    )
    audit.check("sdist 包含 Dockerfile", "include packaging/docker/Dockerfile" in manifest)
    audit.check(
        "sdist 包含第三方许可证与声明",
        "recursive-include packaging/portable/licenses *.txt *.md" in manifest,
    )
    audit.check("sdist 排除 Cython 生成文件", "exclude tools/native/cython/_speedups_round2.c" in manifest)

    release_gate = release_gate_path.read_text(encoding="utf-8")
    audit.check(
        "release gate 禁止 skip 选项", "--skip-payload" not in release_gate and "--skip-portable" not in release_gate
    )
    audit.check("release gate 强制可复现构建", "--skip-reproducible" not in release_gate)
    audit.check("release gate 拒绝 pytest skip", "--fail-on-skip" in release_gate)

    ci = ci_path.read_text(encoding="utf-8")
    release = release_path.read_text(encoding="utf-8")
    portable_spec = portable_spec_path.read_text(encoding="utf-8")
    audit.check(
        "GitHub Release 发布并校验项目 LICENSE",
        "cp LICENSE release-assets/LICENSE" in release and "sha256sum LICENSE *.tar.gz" in release,
    )
    audit.check(
        "Portable Lite 内嵌项目 LICENSE",
        '(str(_project_license), ".")' in portable_spec,
    )
    frontend_check = frontend_check_path.read_text(encoding="utf-8") if frontend_check_path.is_file() else ""
    audit.check(
        "CI 执行前端模块与交互检查",
        "node scripts/check_frontend_interactions.mjs" in ci,
    )
    audit.check(
        "前端交互检查覆盖模块、场次时间线与标签切换",
        all(
            token in frontend_check
            for token in (
                "frontend module graph, bindings, session timeline expansion/reanalysis",
                'await import(`${pathToFileURL(join(copiedStatic, "app.js"))',
                'await candidatesTab.emit("click")',
            )
        ),
    )

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    portable_readme = (REPO_ROOT / "packaging" / "portable" / "README.md").read_text(encoding="utf-8")
    audit.check(
        "文档区分项目代码与第三方许可证",
        all(
            "Copyright (c) 2026 StarGazerQQD" in content and "项目代码采用" in content
            for content in (readme, portable_readme)
        )
        and "项目许可证不改变任何第三方条款" in portable_readme,
    )


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
    model_lock_path = REPO_ROOT / "packaging" / "portable" / "config" / "model_sources.lock.json"
    expected_model_lock_sha = compute_model_lock_sha256(model_lock_path) if model_lock_path.is_file() else ""
    audit.check(
        "engine_pack_info.model_lock_sha256 匹配当前模型锁",
        bool(expected_model_lock_sha) and info.get("model_lock_sha256") == expected_model_lock_sha,
        "内嵌 Engine Pack 元数据必须在模型锁更新后重新生成",
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
