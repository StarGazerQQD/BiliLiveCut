"""版本一致性测试 — 确保所有组件版本统一。

检查所有模块、配置文件和构建脚本的版本号一致性。
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_version_json() -> dict:
    """加载权威版本配置。"""
    p = REPO_ROOT / "packaging" / "portable" / "config" / "version.json"
    return json.loads(p.read_text(encoding="utf-8"))


def test_all_versions_equal_0_1_14_7_alpha() -> None:
    """验证所有版本引用与权威配置一致。"""
    cfg = _load_version_json()
    expected_version = cfg["release_version"]
    expected_label = cfg["version_label"]

    # app/__init__.py
    init_path = REPO_ROOT / "app" / "__init__.py"
    init_content = init_path.read_text(encoding="utf-8")
    line = [line_text for line_text in init_content.split("\n") if "__version__ =" in line_text and '"' in line_text]
    assert line, "app/__init__.py 缺少 __version__"
    assert expected_version in line[0], f"app/__init__.py 版本不匹配: {line[0]} 期望包含 {expected_version}"

    # pyproject.toml
    toml_path = REPO_ROOT / "pyproject.toml"
    toml_content = toml_path.read_text(encoding="utf-8")
    assert f'version = "{expected_version}"' in toml_content, "pyproject.toml 版本不匹配"

    # setup.py
    setup_path = REPO_ROOT / "setup.py"
    setup_content = setup_path.read_text(encoding="utf-8")
    assert f'version="{expected_version}"' in setup_content, "setup.py 版本不匹配"

    # setup_c.py
    setup_c_path = REPO_ROOT / "setup_c.py"
    setup_c_content = setup_c_path.read_text(encoding="utf-8")
    base = expected_version.split("-")[0]
    assert f'version="{base}"' in setup_c_content, "setup_c.py 版本不匹配"

    # blc_portable/__init__.py
    blc_init = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "__init__.py"
    blc_init_content = blc_init.read_text(encoding="utf-8")
    assert f'__version__ = "{expected_version}"' in blc_init_content, "blc_portable/__init__.py 版本不匹配"

    # manifest.py
    manifest_path = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "payload" / "manifest.py"
    manifest_content = manifest_path.read_text(encoding="utf-8")
    assert f'RELEASE_VERSION = "{expected_version}"' in manifest_content, "manifest.py 版本不匹配"

    # builders/lite.py
    lite_path = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "builders" / "lite.py"
    lite_content = lite_path.read_text(encoding="utf-8")
    assert f'RELEASE_VERSION = "{expected_version}"' in lite_content, "builders/lite.py 版本不匹配"

    # builders/full.py
    full_path = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "builders" / "full.py"
    full_content = full_path.read_text(encoding="utf-8")
    assert f'RELEASE_VERSION = "{expected_version}"' in full_content, "builders/full.py 版本不匹配"

    # engine_pack/builder.py
    ep_builder_path = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "engine_pack" / "builder.py"
    ep_builder_content = ep_builder_path.read_text(encoding="utf-8")
    assert f'ENGINE_PACK_VERSION = "{expected_version}"' in ep_builder_content, "engine_pack/builder.py 版本不匹配"

    # engine_pack/manifest.py (now uses version_loader function call)
    ep_manifest_path = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "engine_pack" / "manifest.py"
    ep_manifest_content = ep_manifest_path.read_text(encoding="utf-8")
    assert "ENGINE_PACK_VERSION = _ver_ep_version" in ep_manifest_content, (
        "engine_pack/manifest.py 应使用 version_loader"
    )

    # launcher/main.py
    launcher_path = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "launcher" / "main.py"
    launcher_content = launcher_path.read_text(encoding="utf-8")
    assert f'RELEASE_VERSION = "{expected_version}"' in launcher_content, "launcher/main.py 版本不匹配"
    assert f'VERSION = "{expected_label}"' in launcher_content, "launcher/main.py 版本标签不匹配"

    # launcher/runtime_layout.py
    rl_path = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "launcher" / "runtime_layout.py"
    rl_content = rl_path.read_text(encoding="utf-8")
    assert f'RELEASE_VERSION = "{expected_version}"' in rl_content, "runtime_layout.py 版本不匹配"

    # Engine Pack spec
    spec_path = REPO_ROOT / "packaging" / "portable" / "specs" / "portable_launcher.spec"
    spec_content = spec_path.read_text(encoding="utf-8")
    assert expected_version in spec_content, "portable_launcher.spec 版本不匹配"


def test_version_json_is_valid() -> None:
    """验证 version.json 格式有效。"""
    cfg = _load_version_json()
    assert "release_version" in cfg
    assert "source_commit_short" in cfg
    assert "source_commit_full" in cfg
    assert len(cfg["source_commit_short"]) == 7
    assert len(cfg["source_commit_full"]) == 40
    assert cfg["compatible_python"]["min"] in ("3.11",)
    assert cfg["compatible_python"]["max_validated"] in ("3.12",)


def test_runtime_release_id_contains_payload_hash() -> None:
    """验证 Runtime installer 支持内容寻址 Release ID。"""
    installer_path = REPO_ROOT / "packaging" / "portable" / "src" / "blc_portable" / "runtime" / "installer.py"
    content = installer_path.read_text(encoding="utf-8")
    assert "build_release_id" in content, "runtime installer must use content-addressed Release ID"
    assert "payload_hash" in content, "runtime installer must use payload hash"
