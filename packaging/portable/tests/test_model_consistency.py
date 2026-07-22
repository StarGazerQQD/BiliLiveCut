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
            if engine["hub"] == "huggingface":
                assert len(rev) == 40 and all(char in "0123456789abcdef" for char in rev), (
                    f"Engine {engine['engine_id']}: Hugging Face revision must be a full commit: {rev}"
                )

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
            assert sub.get("target_subdir"), "Sub-model missing target_subdir"

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

    def test_all_distributed_components_have_verified_license_evidence(self) -> None:
        catalog = _load_lock()
        required = {"name", "spdx", "source", "evidence_url", "license_file", "verified_at"}
        for engine in catalog["engines"]:
            components = [engine, *engine.get("sub_models", []), *engine.get("third_party_components", [])]
            for component in components:
                license_info = component.get("license", {})
                assert required <= license_info.keys(), f"Incomplete license metadata: {component}"
                assert license_info["redistribution_verified"] is True
                license_path = _portable_dir / license_info["license_file"]
                assert license_path.is_file(), f"Missing license file: {license_path}"

    def test_production_redistribution_gate_passes(self) -> None:
        from blc_portable.engine_pack.builder import validate_redistribution_readiness

        assert validate_redistribution_readiness() == []

    def test_license_materials_are_copied_into_pack(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.builder import copy_license_materials

        copy_license_materials(tmp_path)
        assert (tmp_path / "licenses" / "THIRD_PARTY_NOTICES.md").is_file()
        assert (tmp_path / "licenses" / "MIT.txt").is_file()
        assert (tmp_path / "licenses" / "Apache-2.0.txt").is_file()

    def test_prepared_models_accept_current_locked_layout(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.builder import _get_engines_for_build, validate_prepared_models

        for engine in _get_engines_for_build():
            target = tmp_path / str(engine["target_path"])
            target.mkdir(parents=True)
            for required_file in engine.get("required_files", []):
                required_path = target / str(required_file)
                required_path.parent.mkdir(parents=True, exist_ok=True)
                required_path.write_bytes(b"fixture")
            for sub_model in engine.get("sub_models", []):
                subdir = target / str(sub_model["target_subdir"])
                subdir.mkdir(parents=True)
                (subdir / "model.bin").write_bytes(b"fixture")
            for component in engine.get("third_party_components", []):
                component_dir = target / str(component["target_subdir"])
                component_dir.mkdir(parents=True)
                (component_dir / "config.json").write_text("{}", encoding="utf-8")

        assert validate_prepared_models(tmp_path) == []

    def test_prepared_models_reject_legacy_cam_plus_plus_path(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.builder import _get_engines_for_build, validate_prepared_models

        paraformer = next(engine for engine in _get_engines_for_build() if engine["engine_id"] == "paraformer")
        target = tmp_path / str(paraformer["target_path"])
        target.mkdir(parents=True)
        for required_file in paraformer["required_files"]:
            (target / str(required_file)).write_bytes(b"fixture")
        for subdir_name in ("fsmn-vad", "ct-punc", "cam++"):
            subdir = target / subdir_name
            subdir.mkdir()
            (subdir / "model.bin").write_bytes(b"fixture")

        errors = validate_prepared_models(tmp_path)
        assert any("campplus" in error for error in errors)

    def test_fixture_uses_current_submodel_and_component_layout(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.builder import build_fixture

        build_fixture(tmp_path)
        assert (tmp_path / "models" / "paraformer" / "campplus" / "model_metadata.json").is_file()
        assert (tmp_path / "models" / "funasr_nano" / "Qwen3-0.6B" / "component_metadata.json").is_file()
