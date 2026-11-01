from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core import paths


def test_stored_media_survives_moving_root_and_changing_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = tmp_path / "original"
    original.mkdir()
    target = original / "clips" / "session_1" / "片段.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    monkeypatch.setattr(paths.settings, "storage_root", str(original))
    stored = paths.to_storage_relative(target)
    assert stored == "clips/session_1/片段.mp4"
    moved = tmp_path / "moved"
    assert original.resolve().parent == moved.resolve().parent == tmp_path.resolve()
    shutil.move(str(original), moved)
    monkeypatch.setattr(paths.settings, "storage_root", str(moved))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert paths.resolve_storage_path(stored).read_bytes() == b"media"
    assert paths.resolve_storage_path(str(target)) == target
    assert not paths.resolve_storage_path(str(target)).exists()


@pytest.mark.parametrize("value", ["", ".", "../outside", "raw/../../outside", "C:relative.mp4", "raw\\clip.mp4"])
def test_invalid_storage_reference_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(paths.settings, "storage_root", str(tmp_path / "storage"))
    with pytest.raises(ValueError):
        paths.resolve_storage_path(value)


def test_storage_aliases_cover_relative_and_existing_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths.settings, "storage_root", str(tmp_path))
    target = tmp_path / "raw" / "segment.ts"
    assert {str(target), target.as_posix(), "raw/segment.ts"} <= set(paths.storage_path_aliases(target))
    assert paths.resolve_storage_path("raw/missing.ts") == tmp_path / "raw" / "missing.ts"
    external = tmp_path.parent / "external.mp4"
    assert paths.to_storage_relative(external) == str(external)
    assert paths.resolve_storage_path(external) == external


def test_storage_symlink_escape_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "storage"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setattr(paths.settings, "storage_root", str(root))
    link = root / "raw"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError):
            paths.resolve_storage_path("raw/clip.mp4")
        with pytest.raises(ValueError):
            paths.to_storage_relative(link / "clip.mp4")
        with pytest.raises(ValueError):
            paths.session_raw_dir(1)
        assert list(outside.iterdir()) == []
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()
