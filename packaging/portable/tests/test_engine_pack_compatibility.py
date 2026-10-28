"""真实 ZIP 的包根、跨版本内容兼容及路径安全回归。"""

from __future__ import annotations

import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from blc_portable.archive.safe_zip import safe_extract
from blc_portable.engine_pack.installer import (
    EnginePackValidationError,
    _safe_extract,
    find_local_engine_packs,
    install_from_engine_pack,
)
from engine_pack_helpers import current_tree_manifest


def make_pack(root: Path, *, wrapper: str = "", mutation: str = "") -> Path:
    tree = root / "tree"
    tree.mkdir()
    manifest = current_tree_manifest(tree)
    manifest.update(
        engine_pack_version="0.1.18.5-alpha",
        portable_release_version="0.1.18.5-alpha",
        source_commit="a" * 40,
        source_commit_short="a" * 7,
    )
    if mutation == "revision":
        manifest["engines"][0]["revision"] = "b" * 40
    elif mutation == "hash":
        next(iter(manifest["files"].values()))["sha256"] = "0" * 64
    elif mutation == "path":
        manifest["files"]["../outside"] = next(iter(manifest["files"].values()))
        manifest["total_files"] += 1
    elif mutation == "model_id":
        manifest["engines"][0]["model_id"] = "wrong/repo"
    elif mutation == "component":
        removed = [path for path in manifest["files"] if "/fsmn-vad/" in path]
        assert removed
        for path in removed:
            manifest["total_files"] -= 1
            del manifest["files"][path]
            (tree / path).unlink()
    (tree / "engine-pack-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive = root / "BiliLiveCut-EnginePack-0.1.18.5-alpha.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in tree.rglob("*"):
            if path.is_file():
                zf.write(path, wrapper + path.relative_to(tree).as_posix())
    return archive


@pytest.mark.parametrize("wrapper", ["", "old-pack/"])
def test_old_source_real_zip_installs_offline_in_either_supported_layout(tmp_path: Path, wrapper: str) -> None:
    pack = make_pack(tmp_path, wrapper=wrapper)
    result = install_from_engine_pack(tmp_path / "isolated-app", pack, "", "")
    assert result["network_requests"] == 0
    assert len(result["installed_engines"]) == 4


@pytest.mark.parametrize("mutation", ["revision", "hash", "path", "model_id", "component"])
def test_compatible_release_does_not_bypass_content_verification(tmp_path: Path, mutation: str) -> None:
    pack = make_pack(tmp_path, mutation=mutation)
    with pytest.raises(EnginePackValidationError):
        install_from_engine_pack(tmp_path / "isolated-app", pack, "", "")
    assert not (tmp_path / "isolated-app" / "models" / "engine-pack-installed.json").exists()


@pytest.mark.parametrize(
    "members",
    [
        ["models/model.bin"],
        ["engine-pack-manifest.json", "nested/engine-pack-manifest.json"],
        ["one/engine-pack-manifest.json", "two/model.bin"],
        ["one/two/engine-pack-manifest.json"],
        ["ENGINE-PACK-MANIFEST.JSON"],
    ],
)
def test_ambiguous_or_missing_root_is_rejected_before_extract(tmp_path: Path, members: list[str]) -> None:
    pack = tmp_path / "bad.zip"
    with zipfile.ZipFile(pack, "w") as zf:
        for member in members:
            zf.writestr(member, b"{}")
    with pytest.raises(EnginePackValidationError, match="archive_layout"):
        _safe_extract(pack, tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "members",
    [
        ["safe.txt", "../escape/"],
        ["a", "a/b"],
        ["a/b", "a"],
        ["a.txt", "A.TXT"],
        ["Models/a", "models/b"],
        ["dir:ads/file"],
        ["NUL.foo.bar/file"],
    ],
)
def test_zip_checks_every_member_before_first_write(tmp_path: Path, members: list[str]) -> None:
    pack = tmp_path / "bad.zip"
    with zipfile.ZipFile(pack, "w") as zf:
        for member in members:
            zf.writestr(member, b"bad")
    with zipfile.ZipFile(pack) as zf, pytest.raises(RuntimeError):
        safe_extract(zf, tmp_path / "output")
    assert list((tmp_path / "output").rglob("*")) == []


def test_directory_symlink_is_not_ignored(tmp_path: Path) -> None:
    pack = tmp_path / "bad.zip"
    info = zipfile.ZipInfo("dir/")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr(info, "../outside")
    with zipfile.ZipFile(pack) as zf, pytest.raises(RuntimeError, match="符号链接"):
        safe_extract(zf, tmp_path / "output")


def test_discovery_uses_current_then_old_standard_names_and_ignores_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = tmp_path / "BiliLiveCut-EnginePack-0.1.20.2-alpha.zip"
    old = tmp_path / "BiliLiveCut-EnginePack-0.1.18.5-alpha.zip"
    current.touch()
    old.touch()
    unrelated = tmp_path / "_MEI-random"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    assert find_local_engine_packs(tmp_path, current.name) == [current, old]
