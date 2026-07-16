"""模型等价性测试 — 在线/离线 Catalog、Builder/Downloader 布局一致性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_portable_dir = Path(__file__).resolve().parent.parent  # portable/
import sys

if str(_portable_dir / "src") not in sys.path:
    sys.path.insert(0, str(_portable_dir / "src"))
if str(_portable_dir / "config") not in sys.path:
    sys.path.insert(0, str(_portable_dir / "config"))


def _load_lock() -> dict:
    path = _portable_dir / "config" / "model_sources.lock.json"
    if not path.exists():
        pytest.skip("model_sources.lock.json not found")
    return json.loads(path.read_text(encoding="utf-8"))


class TestModelCatalogSingleSource:
    def test_all_engines_have_resolved_revision(self) -> None:
        catalog = _load_lock()
        for engine in catalog["engines"]:
            rev = engine.get("resolved_revision", "")
            assert rev, f"Engine {engine['engine_id']}: resolved_revision empty"
            assert rev not in ("main", "master"), f"Engine {engine['engine_id']}: floating revision {rev}"

    def test_no_legacy_funasr_repo(self) -> None:
        catalog = _load_lock()
        funasr = next(e for e in catalog["engines"] if e["engine_id"] == "funasr_nano")
        assert "FunAudioLLM/Fun-ASR-Nano-2512" in funasr["repository"], f"Wrong funasr repo: {funasr['repository']}"

    def test_paraformer_uses_full_ids(self) -> None:
        catalog = _load_lock()
        para = next(e for e in catalog["engines"] if e["engine_id"] == "paraformer")
        assert "/" in para["repository"], f"Not full repo: {para['repository']}"
        for sub in para.get("sub_models", []):
            assert "/" in sub["repository"], f"Sub-model not full repo: {sub['repository']}"
            assert sub.get("target_subdir"), f"Sub-model missing target_subdir"

    def test_all_submodels_have_explicit_target_subdir(self) -> None:
        catalog = _load_lock()
        for engine in catalog["engines"]:
            for sub in engine.get("sub_models", []):
                assert sub.get("target_subdir"), f"{engine['engine_id']} sub-model missing target_subdir"

    def test_no_duplicate_engine_ids(self) -> None:
        catalog = _load_lock()
        ids = [e["engine_id"] for e in catalog["engines"]]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {ids}"

    def test_no_duplicate_target_paths(self) -> None:
        catalog = _load_lock()
        paths = [e["target_path"] for e in catalog["engines"]]
        assert len(paths) == len(set(paths)), f"Duplicate paths: {paths}"

    def test_downloader_uses_catalog(self) -> None:
        downloader_py = _portable_dir / "src" / "blc_portable" / "engine_pack" / "downloader.py"
        content = downloader_py.read_text(encoding="utf-8")
        assert "_load_engine_defs" in content, "downloader.py does not use catalog loader"
        assert "ENGINES: list" not in content, "downloader.py still has independent ENGINES list"
