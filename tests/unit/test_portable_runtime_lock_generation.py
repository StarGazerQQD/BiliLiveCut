from __future__ import annotations

import argparse
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts import generate_portable_runtime_locks as generator
from scripts.audit_portable_runtime_locks import locked_dependencies


def _wheel(root: Path, name: str, version: str, dependencies: tuple[str, ...] = ()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    wheel = root / f"{name}-{version}-py3-none-any.whl"
    info = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{info}/METADATA", metadata)
        archive.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{info}/RECORD", "")
    return wheel


@pytest.mark.parametrize("target", generator.TARGETS, ids=lambda target: target.abi)
@pytest.mark.parametrize("targeted", [False, True])
def test_real_pip_resolution_preserves_only_requested_baseline_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: generator.Target, targeted: bool
) -> None:
    wheelhouse = tmp_path / "wheels"
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    for name in ("application", "dependency"):
        for version in ("1.0", "2.0"):
            wheel = _wheel(wheelhouse, name, version, ("dependency>=1",) if name == "application" else ())
            if version == "1.0":
                shutil.copy2(wheel, baseline)
    old_lock = tmp_path / target.lock_filename
    old_lock.write_text(generator.render_lock(target, baseline), encoding="utf-8")
    requirements = tmp_path / "runtime.in"
    requirements.write_text("application==2.0\n", encoding="utf-8")
    constraints = None
    if targeted:
        constraints = tmp_path / "constraints.txt"
        constraints.write_text(generator.retained_constraints(old_lock, frozenset({"application"})), encoding="utf-8")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.delenv("PIP_FIND_LINKS", raising=False)
    monkeypatch.delenv("PIP_CONSTRAINT", raising=False)
    output = tmp_path / "resolved"
    generator.resolve_target(target, requirements, wheelhouse, output, constraints=constraints)
    new_lock = tmp_path / "resolved.lock"
    new_lock.write_text(generator.render_lock(target, output), encoding="utf-8")
    generator.verify_direct_requirements(requirements, new_lock.read_text(encoding="utf-8"), target)
    assert locked_dependencies(new_lock) == frozenset(
        {("application", "2.0"), ("dependency", "1.0" if targeted else "2.0")}
    )
    if targeted:
        retained = next(line for line in old_lock.read_text().splitlines() if line.startswith("dependency=="))
        assert retained in new_lock.read_text().splitlines()


def test_targeted_upgrade_rejects_real_dependency_conflict_without_rewriting_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse = tmp_path / "wheels"
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    for name in ("application", "dependency"):
        wheel = _wheel(wheelhouse, name, "1.0")
        shutil.copy2(wheel, baseline)
    _wheel(wheelhouse, "application", "2.0", ("dependency>=2",))
    _wheel(wheelhouse, "dependency", "2.0")
    target = generator.TARGETS[0]
    old_lock = tmp_path / target.lock_filename
    original = generator.render_lock(target, baseline)
    old_lock.write_text(original, encoding="utf-8")
    constraints = tmp_path / "constraints.txt"
    constraints.write_text(generator.retained_constraints(old_lock, frozenset({"application"})), encoding="utf-8")
    requirements = tmp_path / "runtime.in"
    requirements.write_text("application==2.0\n", encoding="utf-8")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.delenv("PIP_FIND_LINKS", raising=False)
    monkeypatch.delenv("PIP_CONSTRAINT", raising=False)
    with pytest.raises(subprocess.CalledProcessError):
        generator.resolve_target(target, requirements, wheelhouse, tmp_path / "resolved", constraints=constraints)
    assert old_lock.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("missing", ["py311", "py312", "package"])
def test_targeted_generation_requires_all_baselines_before_building(tmp_path: Path, missing: str) -> None:
    wheelhouse = tmp_path / "baseline"
    _wheel(wheelhouse, "application", "1.0")
    requirements = tmp_path / "runtime.in"
    requirements.write_text("application==2.0\n", encoding="utf-8")
    originals: dict[Path, bytes] = {}
    for target in generator.TARGETS:
        if target.abi != missing:
            path = tmp_path / target.lock_filename
            path.write_text(generator.render_lock(target, wheelhouse), encoding="utf-8")
            originals[path] = path.read_bytes()
    with pytest.raises(RuntimeError, match="absent from" if missing == "package" else "existing lock"):
        generator.generate_locks(
            requirements,
            tmp_path,
            tmp_path / "cache",
            upgrade_packages=frozenset({"unknown" if missing == "package" else "application"}),
        )
    assert not (tmp_path / "cache").exists()
    assert all(path.read_bytes() == content for path, content in originals.items())


@pytest.mark.parametrize(
    "value", ["package>=2", "package[extra]", "package; python_version>'3.11'", "https://example.test"]
)
def test_upgrade_selector_accepts_only_package_names(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        generator.upgrade_package_name(value)


def test_upgrade_selector_normalizes_names_and_can_repeat() -> None:
    args = generator.parse_args(["--upgrade-package", "Hydra_Core", "--upgrade-package", "msgpack"])
    assert args.upgrade_package == ["hydra-core", "msgpack"]
