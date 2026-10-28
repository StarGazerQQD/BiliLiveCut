"""GPU runtime installation retains offline and immutable-dependency guarantees."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_portable_src = Path(__file__).resolve().parents[1] / "src"
if str(_portable_src) not in sys.path:
    sys.path.insert(0, str(_portable_src))

from blc_portable.launcher import torch_runtime as runtime

LOCKS = Path(__file__).resolve().parents[1] / "locks"
REPO = LOCKS.parents[2]


@pytest.fixture
def base_lock(tmp_path: Path) -> Path:
    for path in LOCKS.glob("requirements-torch-*-py312-win-x64.lock"):
        shutil.copy2(path, tmp_path / path.name)
    shutil.copy2(LOCKS / "requirements-cublas-win-x64.lock", tmp_path)
    base = tmp_path / "requirements-runtime-py312-win-x64.lock"
    base.write_text(
        "torch==2.13.0 --hash=sha256:" + "a" * 64 + "\n"
        "torchaudio==2.11.0 --hash=sha256:" + "b" * 64 + "\n"
        "example==1.0 --hash=sha256:" + "c" * 64 + "\n"
    )
    return base


@pytest.mark.parametrize("abi", ["311", "312"])
@pytest.mark.parametrize("choice", ["cu126", "cu130"])
def test_all_official_cuda_locks_match_base_versions_and_abi(abi: str, choice: str) -> None:
    wheels = runtime.cuda_wheels(LOCKS / f"requirements-runtime-py{abi}-win-x64.lock", choice)
    assert {wheel.package for wheel in wheels} == {"torch", "torchaudio"}
    assert all(f"cp{abi}-cp{abi}-win_amd64.whl" in wheel.filename for wheel in wheels)
    assert all(len(wheel.sha256) == 64 for wheel in wheels)


def test_recognized_manual_cuda_pair_is_preserved(base_lock: Path) -> None:
    selected = runtime.choose_torch_runtime(
        base_lock, {"torch": "2.13.0+cu126", "torchaudio": "2.11.0+cu126"}, saved=None, requested=None
    )
    assert selected == "cu126"


@pytest.mark.parametrize(
    "installed",
    [
        {"torch": "2.13.0+cu126", "torchaudio": "2.11.0"},
        {"torch": "2.13.0+cu130", "torchaudio": "2.11.0+cu126"},
        {"torch": "2.12.0+cu126", "torchaudio": "2.11.0+cu126"},
    ],
)
def test_unknown_or_mixed_cuda_pair_is_not_silently_overwritten(base_lock: Path, installed: dict[str, str]) -> None:
    with pytest.raises(RuntimeError, match="为避免覆盖"):
        runtime.choose_torch_runtime(base_lock, installed, saved=None, requested=None)


def test_explicit_cpu_selection_can_replace_cuda(base_lock: Path) -> None:
    assert runtime.choose_torch_runtime(base_lock, {"torch": "2.13.0+cu130"}, saved="cu126", requested="cpu") == "cpu"


def test_persisted_choice_survives_restart(tmp_path: Path) -> None:
    assert runtime.load_torch_runtime(tmp_path) is None
    runtime.save_torch_runtime(tmp_path, "cu126")
    assert runtime.load_torch_runtime(tmp_path) == "cu126"


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": True, "runtime": "cpu"},
        {"schema": 1, "runtime": "other"},
        {"schema": 1, "runtime": "cu126", "extra": True},
        [],
    ],
)
def test_corrupt_choice_never_defaults_to_cpu(tmp_path: Path, payload: object) -> None:
    path = runtime.runtime_config_path(tmp_path)
    path.parent.mkdir()
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        runtime.load_torch_runtime(tmp_path)


def test_failed_atomic_save_retains_previous_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime.save_torch_runtime(tmp_path, "cpu")

    def fail_replace(source: Path, destination: Path) -> None:
        raise PermissionError("locked configuration")

    monkeypatch.setattr(runtime.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        runtime.save_torch_runtime(tmp_path, "cu126")
    assert runtime.load_torch_runtime(tmp_path) == "cpu"
    assert list((tmp_path / "config").iterdir()) == [runtime.runtime_config_path(tmp_path)]


def test_gpu_overlay_preserves_other_locked_dependencies(base_lock: Path, tmp_path: Path) -> None:
    with runtime.install_lock(base_lock, "cu126", tmp_path, offline=False) as lock:
        text = lock.read_text()
        assert "example==1.0 --hash=sha256:" + "c" * 64 in text
        assert "torch==2.13.0" not in text and "torchaudio==2.11.0" not in text
        assert text.count(" @ https://download-r2.pytorch.org/whl/cu126/") == 2
        assert text.count("--hash=sha256:") == 3
        assert "nvidia-cublas-cu12" not in text
    assert not lock.exists()


def test_offline_gpu_install_requires_local_wheels(base_lock: Path, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="离线 CUDA 安装缺少"):
        with runtime.install_lock(base_lock, "cu126", tmp_path, offline=True):
            pytest.fail("missing offline wheels must not yield an installable lock")


def test_offline_gpu_overlay_contains_only_local_urls_with_original_hashes(base_lock: Path, tmp_path: Path) -> None:
    wheelhouse = tmp_path / "vendor/wheels"
    wheelhouse.mkdir(parents=True)
    wheels = runtime.selected_cuda_wheels(base_lock, "cu126")
    for wheel in wheels:
        (wheelhouse / wheel.filename).write_bytes(b"tampered wheel")
    with runtime.install_lock(base_lock, "cu126", tmp_path, offline=True) as lock:
        text = lock.read_text()
        assert "https://" not in text and text.count(" @ file://") == 2
        assert all(f"--hash=sha256:{wheel.sha256}" in text for wheel in wheels)


@pytest.mark.parametrize(
    "mutation",
    [
        ("download-r2.pytorch.org", "evil.invalid"),
        ("%2Bcu126-cp312", "%2Bcu130-cp312"),
        ("-cp312-cp312-win_amd64", "-cp311-cp311-win_amd64"),
        (" --hash=sha256:", " --trusted-host evil.invalid --hash=sha256:"),
    ],
)
def test_tampered_cuda_catalog_is_rejected(base_lock: Path, mutation: tuple[str, str]) -> None:
    lock = base_lock.with_name("requirements-torch-cu126-py312-win-x64.lock")
    lock.write_text(lock.read_text().replace(*mutation))
    with pytest.raises(ValueError):
        runtime.cuda_wheels(base_lock, "cu126")


@pytest.mark.parametrize("saved", [None, "cu126"])
def test_selected_gpu_restart_does_not_reinstall_cpu(
    base_lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved: str | None
) -> None:
    from blc_portable.launcher import main

    if saved is not None:
        runtime.save_torch_runtime(tmp_path, saved)
    monkeypatch.setattr(main, "_find_lock_file", lambda python: base_lock)
    commands: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        output = (
            json.dumps(
                [
                    {"name": "torch", "version": "2.13.0+cu126"},
                    {"name": "torchaudio", "version": "2.11.0+cu126"},
                    {"name": "example", "version": "1.0"},
                    {"name": "nvidia-cublas-cu12", "version": "12.9.2.10"},
                ]
            )
            if "list" in args
            else "{}"
        )
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(main.subprocess, "run", run)
    main.install_dependencies(Path("python.exe"), tmp_path, source_dir=REPO)
    assert not any("install" in args for args in commands)
    assert any("check" in args for args in commands)
    assert runtime.load_torch_runtime(tmp_path) == "cu126"


@pytest.mark.parametrize("inventory", ["invalid JSON", "{}", '[{"name":"torch","version":null}]'])
def test_invalid_inventory_cannot_trigger_cpu_replacement(
    base_lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inventory: str
) -> None:
    from blc_portable.launcher import main

    monkeypatch.setattr(main, "_find_lock_file", lambda python: base_lock)
    commands: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=inventory, stderr="")

    monkeypatch.setattr(main.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        main.install_dependencies(Path("python.exe"), tmp_path)
    assert not any("install" in args for args in commands)
    assert runtime.load_torch_runtime(tmp_path) is None


def test_gpu_preflight_failure_does_not_commit_new_choice(
    base_lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blc_portable.launcher import main

    runtime.save_torch_runtime(tmp_path, "cpu")
    monkeypatch.setattr(main, "_find_lock_file", lambda python: base_lock)

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "-c" in args and "import torch, torchaudio" in args[args.index("-c") + 1]:
            raise subprocess.CalledProcessError(1, args, stderr="CUDA build mismatch")
        output = (
            json.dumps(
                [
                    {"name": "torch", "version": "2.13.0+cu126"},
                    {"name": "torchaudio", "version": "2.11.0+cu126"},
                    {"name": "example", "version": "1.0"},
                    {"name": "nvidia-cublas-cu12", "version": "12.9.2.10"},
                ]
            )
            if "list" in args
            else "{}"
        )
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(main.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        main.install_dependencies(Path("python.exe"), tmp_path, source_dir=REPO, torch_runtime="cu126")
    assert runtime.load_torch_runtime(tmp_path) == "cpu"


def test_offline_missing_gpu_wheels_stop_before_pip_install(
    base_lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blc_portable.launcher import main

    wheelhouse = tmp_path / "vendor/wheels"
    wheelhouse.mkdir(parents=True)
    (wheelhouse / "cpu-placeholder.whl").write_bytes(b"fixture")
    monkeypatch.setenv("BLC_OFFLINE", "1")
    monkeypatch.setattr(main, "_find_lock_file", lambda python: base_lock)
    commands: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                [
                    {"name": "torch", "version": "2.13.0"},
                    {"name": "torchaudio", "version": "2.11.0"},
                    {"name": "example", "version": "1.0"},
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(main.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="离线 CUDA 安装缺少"):
        main.install_dependencies(Path("python.exe"), tmp_path, torch_runtime="cu126")
    assert not any("install" in args for args in commands)
    assert runtime.load_torch_runtime(tmp_path) is None


@pytest.mark.parametrize("mismatch", ["version", "cuda"])
def test_optimized_interpreter_cannot_bypass_installed_runtime_validation(
    base_lock: Path, tmp_path: Path, mismatch: str
) -> None:
    """Run a real optimized interpreter against controlled package metadata and SDKs."""
    _runtime_source(tmp_path)
    for package, version in runtime.expected_versions(base_lock, "cu126").items():
        metadata = tmp_path / f"{package.replace('-', '_')}-{version}.dist-info"
        metadata.mkdir()
        declared = "0.0.0" if mismatch == "version" else version
        (metadata / "METADATA").write_text(f"Name: {package}\nVersion: {declared}\n", encoding="utf-8")
        (tmp_path / f"{package}.py").write_text(
            "from types import SimpleNamespace\n"
            f"__version__ = {version!r}\n"
            f"version = SimpleNamespace(cuda={'None' if mismatch == 'cuda' else repr('12.6')})\n"
            "cuda = SimpleNamespace(is_available=lambda: False)\n",
            encoding="utf-8",
        )
    environment = {**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONOPTIMIZE": "1"}
    with pytest.raises(subprocess.CalledProcessError) as caught:
        runtime.verify_torch_runtime(Path(sys.executable), base_lock, "cu126", environment, source_dir=tmp_path)
    expected = "package versions mismatch" if mismatch == "version" else "CUDA build mismatch"
    assert expected in caught.value.stderr


@pytest.mark.parametrize("outcome", ["unavailable", "kernel", "success"])
def test_installed_cuda_runtime_requires_real_kernel_protocol(
    base_lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    _runtime_source(tmp_path)
    for package, version in runtime.expected_versions(base_lock, "cu126").items():
        metadata = tmp_path / f"{package.replace('-', '_')}-{version}.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(f"Name: {package}\nVersion: {version}\n", encoding="utf-8")
    (tmp_path / "torchaudio.py").write_text("__version__ = '2.11.0+cu126'\n", encoding="utf-8")
    (tmp_path / "torch.py").write_text(
        f"import sys; sys.path.append({str(REPO)!r})\n"
        "from pathlib import Path\n"
        "from pytest import MonkeyPatch\n"
        "from tests.unit.test_cuda_libraries import install_boundary\n"
        f"boundary = install_boundary(Path({str(tmp_path)!r}), MonkeyPatch())\n"
        "boundary.package('nvidia-cublas-cu12')\n"
        "from types import SimpleNamespace\n"
        "__version__ = '2.13.0+cu126'\n"
        "version = SimpleNamespace(cuda='12.6')\n"
        f"cuda = SimpleNamespace(is_available=lambda: {outcome != 'unavailable'}, device_count=lambda: 1,\n"
        " get_device_name=lambda index: 'RTX 5090', get_device_capability=lambda index: (12, 0),\n"
        " get_arch_list=lambda: ['sm_90'], synchronize=lambda index: None)\n"
        "def zeros(size, *, device):\n"
        + (
            " raise RuntimeError('no kernel image is available for execution on the device')\n"
            if outcome == "kernel"
            else " return SimpleNamespace(add_=lambda value: None)\n"
        ),
        encoding="utf-8",
    )
    environment = {**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONOPTIMIZE": "1"}
    real_run = subprocess.run

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[1:4] == ["-m", "pip", "check"]:
            return subprocess.CompletedProcess(args, 0, stdout="No broken requirements", stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr(runtime.subprocess, "run", run)
    if outcome == "success":
        runtime.verify_torch_runtime(Path(sys.executable), base_lock, "cu126", environment, source_dir=tmp_path)
    else:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            runtime.verify_torch_runtime(Path(sys.executable), base_lock, "cu126", environment, source_dir=tmp_path)
        assert ("no usable GPU" if outcome == "unavailable" else "--torch-runtime cu130") in caught.value.stderr


def _runtime_source(root: Path) -> None:
    core = root / "app/core"
    core.mkdir(parents=True)
    (core / "__init__.py").write_text("", encoding="utf-8")
    (core.parent / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(REPO / "app/core/cuda_libraries.py", core)


def test_gpu_preflight_requires_selected_source_directory(base_lock: Path) -> None:
    with pytest.raises(RuntimeError, match="source directory"):
        runtime.verify_torch_runtime(Path(sys.executable), base_lock, "cu130", os.environ)


@pytest.mark.parametrize("missing", ["nvidia-cublas-cu12", "nvidia-cuda-nvrtc-cu12"])
def test_missing_cuda12_support_is_installed_even_when_torch_versions_match(
    base_lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    from blc_portable.launcher import main

    monkeypatch.setattr(main, "_find_lock_file", lambda python: base_lock)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    wheels = tmp_path / "vendor/wheels"
    wheels.mkdir(parents=True)
    for wheel in runtime.selected_cuda_wheels(base_lock, "cu130"):
        (wheels / wheel.filename).write_bytes(b"external pip boundary")
    installs: list[str] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "install" in args:
            installs.append(Path(args[args.index("-r") + 1]).read_text())
        if "-c" in args and "import torch, torchaudio" in args[args.index("-c") + 1]:
            assert kwargs["env"]["PYTHONPATH"] == str(REPO.resolve())
            assert kwargs["cwd"] == str(REPO.resolve())
        inventory = runtime.expected_versions(base_lock, "cu130")
        inventory.pop(missing)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps([{"name": name, "version": version} for name, version in inventory.items()])
            if "list" in args
            else "{}",
            stderr="",
        )

    monkeypatch.setattr(main.subprocess, "run", run)
    main.install_dependencies(Path("python.exe"), tmp_path, source_dir=REPO, torch_runtime="cu130")
    assert len(installs) == 1 and "nvidia-cublas-cu12 @ file:" in installs[0]


def test_cu130_offline_requires_complete_cuda12_closure(base_lock: Path, tmp_path: Path) -> None:
    wheels = tmp_path / "vendor/wheels"
    wheels.mkdir(parents=True)
    for wheel in runtime.selected_cuda_wheels(base_lock, "cu130"):
        if wheel.package != "nvidia-cuda-nvrtc-cu12":
            (wheels / wheel.filename).write_bytes(b"external pip boundary")
    with pytest.raises(RuntimeError, match="nvidia_cuda_nvrtc"):
        with runtime.install_lock(base_lock, "cu130", tmp_path, offline=True):
            pytest.fail("missing NVRTC must stop offline installation")


def test_cpu_overlay_really_replaces_local_cuda_versions_with_pip(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    hashes: dict[str, str] = {}
    for name in ("torch", "torchaudio"):
        for version in ("1.0", "1.0+cu130"):
            wheel = wheelhouse / f"{name}-{version}-py3-none-any.whl"
            info = f"{name}-{version}.dist-info"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(f"{info}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
                archive.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
                archive.writestr(f"{info}/RECORD", "")
            if version == "1.0":
                hashes[name] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    target = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(target)], check=True, capture_output=True, text=True)
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = [str(python), "-m", "pip", "install", "--no-deps", "--no-index", "--find-links", str(wheelhouse)]
    subprocess.run(
        [*command, "torch===1.0+cu130", "torchaudio===1.0+cu130"], check=True, capture_output=True, text=True
    )
    base = tmp_path / "requirements-runtime-py312-win-x64.lock"
    base.write_text(
        "".join(f"{name}==1.0 --hash=sha256:{digest}\n" for name, digest in hashes.items()), encoding="utf-8"
    )
    with runtime.install_lock(base, "cpu", tmp_path, offline=True) as lock:
        subprocess.run([*command, "--require-hashes", "-r", str(lock)], check=True, capture_output=True, text=True)
    result = subprocess.run(
        [str(python), "-c", "import importlib.metadata as m; print(m.version('torch'), m.version('torchaudio'))"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "1.0 1.0"
