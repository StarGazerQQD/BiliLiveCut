"""BiliLiveCut Portable Launcher — 从 EXE 内置 Payload 释放源码并启动。

Startup flow:
1. Check persistent Runtime → if installed and intact, launch directly
2. Read EXE built-in Payload (source_payload.zip + payload_manifest.json)
3. Verify Payload SHA-256
4. Atomic install to runtime/releases/<release-id>/
5. Python/deps/FFmpeg detection
6. 模型准备: installed -> Engine Pack -> online download
7. Launch app.cli serve
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from blc_portable.console import configure_console_encoding

# -- Constants ──────────────────────────────────────────────────
APP_NAME = "BiliLiveCut"
VERSION = "V0.1.17.4 Alpha"
RELEASE_VERSION = "0.1.17.4-alpha"
SOURCE_COMMIT_SHORT = "abae819"
# NOTE: RELEASE_ID 将在获得 Payload SHA-256 后动态生成 (内容寻址)
SUPPORTED_PYTHON_VERSIONS = frozenset({(3, 11), (3, 12)})

VENV_DIR = ".venv"
WHEELS_DIR = os.path.join("vendor", "wheels")

# China mirror
PIP_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
PIP_EXTRA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_TRUSTED_HOSTS = ["mirrors.aliyun.com", "pypi.tuna.tsinghua.edu.cn"]

# FFmpeg
FFMPEG_WIN_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"


# -- Resource paths ──────────────────────────────────────────────


def get_bundled_resource_path(rel: str) -> Path | None:
    """Get bundled resource path, compatible with PyInstaller and normal run。

    :param rel: relative path。
    :returns: 资源路径，not found返回 None。
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent.parent.parent.parent / "dist" / "payload"

    p = base / rel
    return p if p.exists() else None


def get_payload_zip() -> Path:
    """Get embedded Payload ZIP path。

    :returns: Payload ZIP 路径。
    :raises RuntimeError: 找不到时。
    """
    p = get_bundled_resource_path("source_payload.zip")
    if p is None:
        raise RuntimeError("Built-in Payload not found (source_payload.zip). Ensure EXE embeds Payload correctly.")
    return p


def get_payload_manifest() -> dict[str, Any]:
    """Read and validate the current embedded Payload Manifest。

    :returns: Manifest dict。
    :raises RuntimeError: 找不到时。
    """
    p = get_bundled_resource_path("payload_manifest.json")
    if p is None:
        raise RuntimeError("Built-in Manifest not found (payload_manifest.json).")
    try:
        manifest = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Built-in Manifest is unreadable: {exc}") from exc
    from blc_portable.payload.manifest import MANIFEST_FORMAT_VERSION, validate_manifest_schema

    errors = validate_manifest_schema(manifest)
    if errors or manifest["format_version"] != MANIFEST_FORMAT_VERSION:
        raise RuntimeError("Built-in Manifest does not match the current Payload schema")
    return manifest


def get_engine_pack_info() -> dict[str, Any] | None:
    """Read and validate current embedded Engine Pack metadata。

    :returns: Engine Pack info dict, None if not embedded。
    """
    p = get_bundled_resource_path("engine_pack_info.json")
    if p is None:
        return None
    try:
        from blc_portable.engine_pack.schema import ExternalMetadata

        metadata = ExternalMetadata.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise RuntimeError(f"Embedded Engine Pack metadata is invalid: {exc}") from exc
    errors = metadata.validate()
    if errors:
        raise RuntimeError("Embedded Engine Pack metadata validation failed: " + "; ".join(errors))
    return metadata.to_dict()


# -- Runtime management ──────────────────────────────────────────


def get_app_root() -> Path:
    """Get Portable app root dir。委托给 runtime 模块。"""
    from blc_portable.runtime import get_app_root as _get

    return _get()


def get_releases_dir() -> Path:
    """Get releases dir。委托给 runtime 模块。"""
    from blc_portable.runtime import get_releases_dir as _get

    return _get()


def get_current_release_dir() -> Path | None:
    """Get currently active Release dir。委托给 runtime 模块。"""
    from blc_portable.runtime import get_current_release_dir as _get

    return _get()


def install_source_from_payload(app_root: Path) -> Path:
    """Atomic install from embedded Payload to Runtime。

    :param app_root: app root dir。
    :returns: installed Release dir。
    """
    import hashlib

    manifest = get_payload_manifest()
    zip_path = get_payload_zip()

    hasher = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    expected_hash = hasher.hexdigest()

    from blc_portable.runtime.installer import install_from_payload as _installer

    return _installer(
        app_root,
        zip_path,
        manifest,
        expected_hash=expected_hash,
        expected_version=RELEASE_VERSION,
        expected_commit=SOURCE_COMMIT_SHORT,
    )


def ensure_data_dirs(app_root: Path) -> None:
    """ensure persistent data dirs存在。

    :param app_root: app root dir。
    """
    for d in ["data", "storage", "models", "vendor", "bin", "logs"]:
        (app_root / d).mkdir(parents=True, exist_ok=True)


def ensure_env(app_root: Path, source_dir: Path) -> None:
    """如果 .env not found，从模板创建。

    :param app_root: app root dir。
    :param source_dir: source dir。
    """
    env_path = app_root / ".env"
    if env_path.exists():
        return

    template = source_dir / ".env.example"
    if not template.is_file():
        raise FileNotFoundError(f"Portable source is missing required environment template: {template}")

    template_lines = template.read_text(encoding="utf-8").splitlines()
    launcher_only_prefixes = ("PIP_INDEX_URL=", "PIP_EXTRA_INDEX_URL=")
    app_lines = [line for line in template_lines if not line.startswith(launcher_only_prefixes)]
    env_path.write_text("\n".join(app_lines) + "\n", encoding="utf-8")
    print("  .env created from template")


# -- Environment prep ──────────────────────────────────────────────


def _inspect_python(command: Sequence[str]) -> tuple[Path, tuple[int, int]] | None:
    """Return the real interpreter path and version for one command."""
    probe = "import json,sys; print(json.dumps({'executable': sys.executable, 'version': list(sys.version_info[:2])}))"
    try:
        result = subprocess.run(
            [*command, "-c", probe],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        payload = json.loads(result.stdout.strip())
        executable = Path(str(payload["executable"]))
        version_raw = payload["version"]
        if (
            not executable.is_file()
            or not isinstance(version_raw, list)
            or len(version_raw) != 2
            or not all(isinstance(part, int) for part in version_raw)
        ):
            return None
        return executable.resolve(), (version_raw[0], version_raw[1])
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _python_version(python: Path) -> tuple[int, int] | None:
    """Return a Python executable's major/minor version when it is runnable."""
    inspected = _inspect_python((str(python),))
    return inspected[1] if inspected else None


def _find_system_python() -> Path | None:
    """Find a system Python interpreter supported by Portable locks."""
    candidates: list[tuple[str, ...]]
    if sys.platform == "win32":
        candidates = [("py", "-3.12"), ("py", "-3.11"), ("python",), ("python3",)]
    else:
        candidates = [("python3",), ("python",)]
    for command in candidates:
        try:
            inspected = _inspect_python(command)
            if inspected and inspected[1] in SUPPORTED_PYTHON_VERSIONS and ".venv" not in str(inspected[0]):
                return inspected[0]
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _find_portable_python(app_root: Path) -> Path | None:
    """Find Python in portable-python dir。

    :param app_root: app root dir。
    :returns: Python 路径。
    """
    pp = app_root / "portable-python"
    if not pp.exists():
        return None
    if sys.platform == "win32":
        candidates = [pp / "python.exe", pp / "python3.exe"]
    else:
        candidates = [pp / "bin" / "python3", pp / "bin" / "python"]
    for c in candidates:
        if c.exists():
            return c
    return None


def _managed_venv_metadata_valid(
    venv_dir: Path,
    expected_version: tuple[int, int] | None = None,
) -> bool:
    """Return whether a managed venv has complete, usable metadata.

    :param venv_dir: Managed virtual-environment directory.
    :param expected_version: Optional runnable interpreter version to match.
    :returns: Whether ``pyvenv.cfg`` identifies an existing interpreter home
        and a syntactically valid, matching Python version.
    """
    config_path = venv_dir / "pyvenv.cfg"
    try:
        fields = {
            key.strip().lower(): value.strip()
            for line in config_path.read_text(encoding="utf-8").splitlines()
            if "=" in line
            for key, value in (line.split("=", 1),)
        }
    except OSError:
        return False
    home_text = fields.get("home", "")
    version_text = fields.get("version", "")
    try:
        parsed_version = tuple(int(part) for part in version_text.split(".")[:2])
    except ValueError:
        return False
    if len(parsed_version) != 2 or not Path(home_text).is_dir():
        return False
    return expected_version is None or parsed_version == expected_version


def prepare_venv(app_root: Path) -> Path:
    """Prepare the application-managed virtual environment.

    A runnable 3.11/3.12 environment is reused.  A runnable environment with
    another ABI is rejected explicitly because replacing it could hide an
    unsupported user/runtime selection.  An incomplete or unreadable
    ``<app_root>/.venv`` is safe to rebuild because that exact directory is
    owned by the Portable launcher.

    :param app_root: app root dir。
    :returns: venv python path。
    """
    venv_dir = app_root / VENV_DIR
    if sys.platform == "win32":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"

    if venv_python.exists():
        existing_version = _python_version(venv_python)
        if existing_version in SUPPORTED_PYTHON_VERSIONS:
            if _managed_venv_metadata_valid(venv_dir, existing_version):
                return venv_python
            print("  managed .venv metadata is incomplete; rebuilding it safely...")
            _remove_managed_venv(app_root, venv_dir)
        elif existing_version is not None:
            version_text = ".".join(str(part) for part in existing_version)
            raise RuntimeError(
                f"Existing virtual environment uses unsupported Python {version_text}. "
                "Only Python 3.11 and 3.12 are supported."
            )
        else:
            print("  managed .venv is unreadable; rebuilding it safely...")
            _remove_managed_venv(app_root, venv_dir)
    elif venv_dir.exists():
        print("  managed .venv is incomplete; rebuilding it safely...")
        _remove_managed_venv(app_root, venv_dir)

    portable_py = _find_portable_python(app_root)
    if portable_py is not None:
        portable_version = _python_version(portable_py)
        if portable_version not in SUPPORTED_PYTHON_VERSIONS:
            version_text = ".".join(str(part) for part in portable_version) if portable_version else "unreadable"
            raise RuntimeError(
                f"Bundled Portable Python is unsupported ({version_text}). Only Python 3.11 and 3.12 are supported."
            )
        system_py = portable_py
    else:
        system_py = _find_system_python()
    if system_py is None:
        raise RuntimeError(
            "Compatible Python 3.11/3.12 not found. Install Python or place Portable Python in portable-python/.\n"
            "Portable Python download: https://www.python.org/downloads/windows/"
        )

    version = _python_version(system_py)
    if version not in SUPPORTED_PYTHON_VERSIONS:
        raise RuntimeError("Selected Python changed during detection. Only Python 3.11 and 3.12 are supported.")
    py_ver = ".".join(str(part) for part in version)

    print(f"  Python: {system_py} ({py_ver})")
    print("  creating venv...")
    command = [str(system_py), "-m", "venv", str(venv_dir)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        if venv_dir.exists():
            _remove_managed_venv(app_root, venv_dir)
        raise RuntimeError(
            _format_process_failure(
                "Virtual environment creation failed",
                system_py,
                returncode=None,
                stdout="",
                stderr="",
                root_exception=f"{type(exc).__name__}: {exc}",
            )
        ) from exc
    if result.returncode != 0:
        if venv_dir.exists():
            _remove_managed_venv(app_root, venv_dir)
        raise RuntimeError(
            _format_process_failure(
                "Virtual environment creation failed",
                system_py,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                root_exception=f"process exited with code {result.returncode}",
            )
        )
    created_version = _python_version(venv_python)
    if created_version not in SUPPORTED_PYTHON_VERSIONS or not _managed_venv_metadata_valid(
        venv_dir,
        created_version,
    ):
        if venv_dir.exists():
            _remove_managed_venv(app_root, venv_dir)
        raise RuntimeError(
            "Virtual environment creation completed without a runnable Python 3.11/3.12 "
            "interpreter and complete pyvenv.cfg metadata."
        )
    return venv_python


def _remove_managed_venv(app_root: Path, candidate: Path) -> None:
    """Remove only the exact ``<app_root>/.venv`` directory.

    :param app_root: Portable application root.
    :param candidate: Directory proposed for removal.
    :raises RuntimeError: If the candidate is outside the managed location.
    """
    expected = (app_root.resolve() / VENV_DIR).resolve()
    actual = candidate.resolve()
    if actual != expected:
        raise RuntimeError(f"Refusing to remove non-managed virtual environment: {candidate}")
    if candidate.exists():
        shutil.rmtree(candidate)


def _format_process_failure(
    title: str,
    interpreter: Path,
    *,
    returncode: int | None,
    stdout: str | None,
    stderr: str | None,
    root_exception: str,
) -> str:
    """Build one actionable root-cause report for a child Python failure."""
    return "\n".join(
        (
            title,
            f"Interpreter: {interpreter}",
            f"Return code: {returncode if returncode is not None else 'not started'}",
            f"stdout:\n{(stdout or '').strip() or '<empty>'}",
            f"stderr:\n{(stderr or '').strip() or '<empty>'}",
            f"Root exception: {root_exception}",
        )
    )


def _find_lock_file(venv_python: Path) -> Path:
    """Find the ABI-specific lock file, compatible with frozen and source modes.

    :param venv_python: venv python path, used to detect ABI.
    :returns: Path to the lock file.
    :raises RuntimeError: When no matching lock file exists.
    """
    r = subprocess.run(
        [str(venv_python), "-c", "import sys; v=sys.version_info[:2]; print(f'py{v[0]}{v[1]}')"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    abi = r.stdout.strip()
    lock_name = f"requirements-runtime-{abi}-win-x64.lock"

    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        candidate = base / "locks" / lock_name
        if candidate.exists():
            return candidate

    # Source/test mode
    candidate = Path(__file__).resolve().parent.parent.parent.parent / "locks" / lock_name
    if candidate.exists():
        return candidate

    raise RuntimeError(f"Lock file not found: {lock_name}\nOnly Python 3.11 and 3.12 are supported.")


def _find_local_wheelhouse(app_root: Path) -> Path | None:
    """Find a non-empty Full Bundle wheelhouse under the application root.

    :param app_root: Portable application root directory.
    :returns: The local wheelhouse path, or ``None`` when it is unavailable.
    """
    wheelhouse = app_root / WHEELS_DIR
    if wheelhouse.is_dir() and any(wheelhouse.glob("*.whl")):
        return wheelhouse
    return None


def _find_bootstrap_wheelhouse() -> Path | None:
    """Find the minimal wheel set embedded in Lite for online installation."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent.parent.parent.parent / "dist"
    wheelhouse = base / "bootstrap-wheels"
    if wheelhouse.is_dir() and any(wheelhouse.glob("*.whl")):
        return wheelhouse
    return None


def _run_dependency_preflight(venv_python: Path) -> None:
    """Validate the supported ABI and all provisioning/runtime imports once.

    The check deliberately runs as one child process so a first-launch failure
    has one root report instead of a cascade of per-module warnings.

    :param venv_python: Python executable from the managed virtual environment.
    :raises RuntimeError: If Python or any required module is unavailable.
    """
    script = (
        "import sys; "
        "supported={(3,11),(3,12)}; version=sys.version_info[:2]; "
        "assert version in supported, f'unsupported Python {version[0]}.{version[1]}'; "
        "import fastapi; import uvicorn; import sqlmodel; import pydantic; "
        "import playwright; import openai; import huggingface_hub; import modelscope; "
        "print(f'Python {version[0]}.{version[1]}; 8 modules')"
    )
    command = [str(venv_python), "-c", script]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            _format_process_failure(
                "Dependency preflight failed",
                venv_python,
                returncode=None,
                stdout="",
                stderr="",
                root_exception=f"{type(exc).__name__}: {exc}",
            )
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            _format_process_failure(
                "Dependency preflight failed",
                venv_python,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                root_exception=f"child preflight exited with code {result.returncode}",
            )
        )
    print(f"  dependency preflight OK: {(result.stdout or '').strip() or 'Python and 8 modules'}")


def _run_import_smoke(venv_python: Path, module: str, source_dir: Path | None = None) -> None:
    """Import one runtime module and expose the original failure details.

    ``app.cli`` must be imported from the installed content-addressed Runtime,
    never from the checkout or another ambient ``PYTHONPATH`` entry.

    :param venv_python: Python executable from the prepared virtual environment.
    :param module: Module name to import.
    :param source_dir: Installed Runtime source directory, required for ``app.cli``.
    :raises RuntimeError: If the import fails or resolves outside ``source_dir``.
    """
    env: dict[str, str] | None = None
    cwd: str | None = None
    script = f"import {module}; print('  ok: {module}')"

    if module == "app.cli":
        if source_dir is None:
            raise RuntimeError("app.cli import smoke requires the installed Runtime source directory")
        resolved_source = source_dir.resolve()
        if not (resolved_source / "app" / "cli.py").is_file():
            raise RuntimeError(f"Installed Runtime is missing app/cli.py: {resolved_source}")
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(resolved_source) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
        cwd = str(resolved_source)
        script = (
            "from pathlib import Path; import app.cli; "
            f"_expected = Path({str(resolved_source)!r}); "
            "_actual = Path(app.cli.__file__).resolve(); "
            "assert _actual.is_relative_to(_expected), "
            "f'app.cli loaded from unexpected path: {_actual}'; "
            "print('  ok: app.cli', _actual)"
        )

    try:
        result = subprocess.run(
            [str(venv_python), "-c", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        details = "\n".join(part.strip() for part in (exc.stdout or "", exc.stderr or "") if part and part.strip())
        if not details:
            details = f"process exited with code {exc.returncode}"
        raise RuntimeError(f"Import smoke check failed for {module}:\n{details}") from exc

    output = (result.stdout or "").strip()
    print(output or f"  ok: {module}")


def _build_service_command(venv_python: Path) -> list[str]:
    """Build the service command without relying on ``app.cli`` module execution."""
    return [
        str(venv_python),
        "-c",
        "from app.cli import app; app()",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]


def install_dependencies(
    venv_python: Path,
    app_root: Path,
    *,
    source_dir: Path | None = None,
) -> None:
    """Install Python dependencies from ABI-specific lock file.

    :param venv_python: venv python path.
    :param app_root: app root dir.
    :param source_dir: Installed Runtime source used for the ``app.cli`` smoke check.
    """
    lock_file = _find_lock_file(venv_python)
    needs_install = True

    # Check if pip freeze output matches lock (skip full hash comparison, do version check).
    # ``--all`` is required because pip otherwise omits bootstrap tools, including itself.
    try:
        raw_freeze = subprocess.run(
            [str(venv_python), "-m", "pip", "freeze", "--all"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
        installed = {}
        for line in raw_freeze.strip().split("\n"):
            if "==" in line:
                pkg_ver = line.strip().split("==", 1)
                pkg = re.sub(r"[-_.]+", "-", pkg_ver[0]).lower()
                installed[pkg] = pkg_ver[1]
        missing = []
        with open(lock_file, encoding="utf-8") as lf:
            for line in lf:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("--"):
                    continue
                if "==" not in line:
                    continue
                raw_pkg = line.split("==")[0].strip().lower()
                # Strip extras like [standard] from package name
                pkg = re.sub(r"[-_.]+", "-", raw_pkg.split("[")[0])
                raw_ver = line.split("==", 1)[1].split(";", 1)[0].split("\\", 1)[0].strip().split()[0]
                actual_ver = installed.get(pkg)
                if actual_ver is None or actual_ver != raw_ver:
                    missing.append(f"{raw_pkg}: expected {raw_ver}, got {actual_ver}")
        if not missing:
            print("  dependencies already installed (version match)")
            needs_install = False
        else:
            print(f"  dependencies outdated or missing ({len(missing)}), re-installing...")
    except (subprocess.CalledProcessError, OSError):
        pass

    if needs_install:
        # Install from lock file with mandatory hash verification
        print(f"  install deps (lock file: {lock_file.name})...")
        wheelhouse = _find_local_wheelhouse(app_root)
        if wheelhouse is not None:
            print(f"  local wheelhouse detected, enforcing offline install: {wheelhouse}")
            install_source_flags = ["--no-index", "--find-links", str(wheelhouse)]
        elif (app_root / "portable-python" / "python.exe").is_file():
            raise RuntimeError(
                "Full Bundle wheelhouse is missing or empty: "
                f"{app_root / WHEELS_DIR}\nRefusing to download dependencies from the network."
            )
        elif os.environ.get("PIP_NO_INDEX") == "1":
            install_source_flags = ["--no-index"]
        else:
            bootstrap_wheelhouse = _find_bootstrap_wheelhouse()
            if bootstrap_wheelhouse is None:
                raise RuntimeError(
                    "Lite bootstrap wheelhouse is missing. Re-download the official Lite executable; "
                    "source-only dependencies cannot be installed safely from PyPI sdists."
                )
            print(f"  embedded Lite bootstrap wheels: {bootstrap_wheelhouse}")
            install_source_flags = ["--find-links", str(bootstrap_wheelhouse)]
        subprocess.run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "-r",
                str(lock_file),
                "--require-hashes",
                "--only-binary=:all:",
            ]
            + install_source_flags,
            check=True,
            timeout=600,
        )

    # Always run one import/ABI preflight, including on subsequent launches
    # where package versions already match.  Provisioning dependencies are
    # part of the same root-cause report.
    print("  dependency preflight...")
    _run_dependency_preflight(venv_python)
    if source_dir is not None:
        _run_import_smoke(venv_python, "app.cli", source_dir)
    print("  deps install complete")


# -- Model preparation ──────────────────────────────────────────────


def _provisioner_paths() -> tuple[Path, Path]:
    """Return import and configuration roots for the external provisioner."""
    if getattr(sys, "frozen", False):
        bundle_root = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        source_root = bundle_root / "provisioner"
        config_root = bundle_root / "provisioner-config"
    else:
        portable_root = Path(__file__).resolve().parents[3]
        source_root = portable_root / "src"
        config_root = portable_root / "config"
    if not (source_root / "blc_portable" / "launcher" / "model_downloader.py").is_file():
        raise RuntimeError(f"Provisioning helper source is missing: {source_root}")
    if not (config_root / "model_sources.lock.json").is_file():
        raise RuntimeError(f"Provisioning model configuration is missing: {config_root}")
    return source_root, config_root


def prepare_models(
    venv_python: Path,
    app_root: Path,
    user_engine_pack_path: str | None = None,
    *,
    offline: bool = False,
    fallback_online: bool = False,
) -> dict[str, Any]:
    """Run all model provisioning in the managed virtual environment.

    The frozen launcher is only an orchestrator.  Third-party model SDKs and
    all provisioning code execute under ``venv_python`` after dependency
    installation has completed.

    :param venv_python: Managed virtual-environment interpreter.
    :param app_root: Portable application root.
    :param user_engine_pack_path: Optional explicitly selected Engine Pack.
    :param offline: Block online model downloads.
    :param fallback_online: Allow an invalid explicit pack to fall back online.
    :returns: Structured provisioning result.
    """
    pack_info = get_engine_pack_info()
    if pack_info is None:
        from blc_portable.engine_pack.manifest import ARCHIVE_FILENAME, ENGINE_PACK_VERSION

        pack_info = {
            "engine_pack_version": ENGINE_PACK_VERSION,
            "filename": ARCHIVE_FILENAME,
            "crc32": "",
            "sha256": "",
        }

    source_root, config_root = _provisioner_paths()
    command = [
        str(venv_python),
        "-m",
        "blc_portable.launcher.model_downloader",
        "--app-root",
        str(app_root.resolve()),
        "--config-dir",
        str(config_root.resolve()),
        "--expected-filename",
        str(pack_info["filename"]),
        "--expected-crc32",
        str(pack_info.get("crc32", "")),
        "--expected-sha256",
        str(pack_info.get("sha256", "")),
    ]
    if user_engine_pack_path:
        command.extend(("--engine-pack", str(Path(user_engine_pack_path).resolve())))
    if offline:
        command.append("--offline")
    if fallback_online:
        command.append("--fallback-online")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root.resolve())
    env["BLC_MODEL_CONFIG_DIR"] = str(config_root.resolve())
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=None,
            cwd=str(app_root),
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            _format_process_failure(
                "Model provisioning helper failed",
                venv_python,
                returncode=None,
                stdout="",
                stderr="",
                root_exception=f"{type(exc).__name__}: {exc}",
            )
        ) from exc

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    result_prefix = "BLC_PROVISION_RESULT="
    root_prefix = "BLC_PROVISION_ROOT_EXCEPTION="
    result_line = next((line for line in reversed(stdout.splitlines()) if line.startswith(result_prefix)), "")
    root_line = next((line for line in reversed(stderr.splitlines()) if line.startswith(root_prefix)), "")
    visible_stdout = "\n".join(line for line in stdout.splitlines() if not line.startswith(result_prefix)).strip()
    visible_stderr = "\n".join(line for line in stderr.splitlines() if not line.startswith(root_prefix)).strip()
    if visible_stdout:
        print(visible_stdout)
    if result.returncode != 0:
        root_exception = (
            root_line.removeprefix(root_prefix) if root_line else f"child exited with code {result.returncode}"
        )
        raise RuntimeError(
            _format_process_failure(
                "Model provisioning helper failed",
                venv_python,
                returncode=result.returncode,
                stdout=visible_stdout,
                stderr=visible_stderr,
                root_exception=root_exception,
            )
        )
    if visible_stderr:
        print(visible_stderr, file=sys.stderr)
    if not result_line:
        raise RuntimeError(
            _format_process_failure(
                "Model provisioning helper returned no result",
                venv_python,
                returncode=result.returncode,
                stdout=visible_stdout,
                stderr=visible_stderr,
                root_exception="missing BLC_PROVISION_RESULT record",
            )
        )
    try:
        payload = json.loads(result_line.removeprefix(result_prefix))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            _format_process_failure(
                "Model provisioning helper returned invalid JSON",
                venv_python,
                returncode=result.returncode,
                stdout=visible_stdout,
                stderr=visible_stderr,
                root_exception=f"{type(exc).__name__}: {exc}",
            )
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Model provisioning helper result must be a JSON object")
    return payload


# -- Launch ──────────────────────────────────────────────────


def _pause_before_exit() -> None:
    """Pause for an interactive console without failing on closed stdin."""
    if sys.stdin is None or not sys.stdin.isatty():
        return
    print("Press Enter to exit...")
    try:
        input()
    except EOFError:
        pass


def _fail(msg: str) -> None:
    """Print error and exit。

    :param msg: 错误消息。
    """
    print()
    print("*" * 60)
    for line in msg.strip().split("\n"):
        print(f"  [Error] {line}")
    print("*" * 60)
    print()
    _pause_before_exit()
    sys.exit(1)


def _run_doctor(app_root: Path) -> int:
    """Run System Diagnostics check。

    检查项:
    - Runtime 是否已安装
    - models/ 是否完整
    - Python/venv 可用性
    - 关键文件存在性
    - 校验信息完整性

    :param app_root: app root dir。
    """
    import platform

    print("=" * 60)
    print(f"  {APP_NAME} Doctor — System Diagnostics")
    print("=" * 60)
    print()

    checks_passed = 0
    checks_warned = 0
    checks_failed = 0

    def _check(
        name: str, condition: bool, detail: str = "", expected: str = "", actual: str = "", suggestion: str = ""
    ) -> None:
        nonlocal checks_passed, checks_warned, checks_failed
        if condition:
            status = "[PASS]"
            checks_passed += 1
        else:
            status = "[FAIL]"
            checks_failed += 1
        msg = f"  {status} {name}"
        if detail:
            msg += f": {detail}"
        print(msg)
        if not condition and expected:
            print(f"         expected: {expected}")
        if not condition and actual:
            print(f"         actual: {actual}")
        if not condition and suggestion:
            print(f"         suggestion: {suggestion}")

    def _warn(name: str, detail: str = "") -> None:
        nonlocal checks_warned
        checks_warned += 1
        print(f"  [WARN] {name}: {detail}")

    # 1. Runtime
    current = get_current_release_dir()
    _check("Runtime installed", current is not None, str(current) if current else "not installed")

    # 2. Payload
    try:
        manifest = get_payload_manifest()
        _check("Payload Manifest readable", True, f"v{manifest.get('portable_release_version')}")
    except RuntimeError:
        _check("Payload Manifest readable", False, "not readable")

    # 3. Engine Pack 信息
    ep = get_engine_pack_info()
    if ep is None:
        _warn("Engine Pack info embedded", "not embedded; an externally verified Engine Pack is supported")
    else:
        _check("Engine Pack info embedded", True)
        _check("Engine Pack CRC32 non-empty", bool(ep.get("crc32")), str(ep.get("crc32"))[:12])
        _check(
            "Engine Pack SHA-256 non-empty",
            bool(ep.get("sha256")),
            str(ep.get("sha256"))[:12] if ep.get("sha256") else "empty",
        )

    # 4. Python 可用
    pp = _find_portable_python(app_root)
    pp_version = _python_version(pp) if pp else None
    if pp is None:
        _warn("Portable Python", "not bundled (expected for Lite mode)")
    else:
        version_text = ".".join(str(part) for part in pp_version) if pp_version else "unreadable"
        _check(
            "Portable Python 3.11/3.12",
            pp_version in SUPPORTED_PYTHON_VERSIONS,
            f"{pp} ({version_text})",
        )

    py = _find_system_python()
    if py is None:
        _warn("System Python 3.11/3.12", "not found or unsupported")
    else:
        _check("System Python 3.11/3.12", True, str(py))

    compatible_python = pp if pp_version in SUPPORTED_PYTHON_VERSIONS else py
    _check(
        "Compatible Python runtime",
        compatible_python is not None,
        str(compatible_python) if compatible_python else "not found",
    )

    # 5. Models
    models_dir = app_root / "models"
    if models_dir.exists():
        for engine_id in ("whisper", "paraformer", "sensevoice", "funasr_nano"):
            epath = models_dir / engine_id
            _check(
                f"engine {engine_id}",
                epath.exists() and any(epath.iterdir()),
                f"{sum(1 for _ in epath.rglob('*') if _.is_file()) if epath.exists() else 0} files",
            )
    else:
        _check("models dir", False, "not found")

    # 6. FFmpeg
    ffmpeg = app_root / "bin" / "ffmpeg.exe"
    _check("FFmpeg", ffmpeg.exists(), str(ffmpeg) if ffmpeg.exists() else "not found")

    # 7. 平台信息
    print()
    print(f"  Python: {platform.python_version()}")
    print(f"  Platform: {platform.platform()}")
    print(f"  Architecture: {'x64' if sys.maxsize > 2**32 else 'x86'}")

    print()
    print(f"  Diagnostics complete: {checks_passed} PASS, {checks_warned} WARN, {checks_failed} FAIL")
    return 0 if checks_failed == 0 else 1


def _verify_installed_models(app_root: Path) -> None:
    """验证已安装模型完整性（per-file SHA-256 recompute）。

    :param app_root: app root dir。
    """
    from blc_portable.engine_pack.installer import _read_installed_manifest, check_installed_models

    models_dir = app_root / "models"
    installed = _read_installed_manifest(models_dir)

    if installed is None:
        print("  [FAIL] installed model manifest not found")
        sys.exit(1)

    print("=" * 60)
    print("  Model Integrity Verification")
    print("=" * 60)
    print(f"  Model set fingerprint: {installed.get('model_set_fingerprint', '<legacy>')}")
    print(f"  installed at: {installed.get('installed_at')}")
    records = installed.get("engines", {})
    print(f"  engines: {sorted(records) if isinstance(records, dict) else '<invalid>'}")
    print()
    ok, errors = check_installed_models(models_dir, full_rehash=True)
    if not ok:
        for error in errors:
            print(f"  [FAIL] {error}")
        sys.exit(1)
    for engine_id, info in sorted(records.items()):
        print(
            f"  [PASS] engine {engine_id}: {info.get('file_count', 0)} files, "
            f"fingerprint={str(info.get('content_fingerprint', ''))[:16]}..."
        )
    print(f"\n  [PASS] All {len(records)} engines verified by SHA-256")


def _repair_runtime(app_root: Path) -> None:
    """Clear old Runtime to trigger reinstall。"""
    from blc_portable.runtime.activation import delete_current_json

    delete_current_json(app_root)
    releases = app_root / "runtime" / "releases"
    if releases.exists():
        shutil.rmtree(releases)
    print("[Repair] cleared old Runtime, will reinstall")


def build_parser() -> argparse.ArgumentParser:
    """Build command-line argument parser。

    :returns: ArgumentParser 实例。
    """
    import argparse

    parser = argparse.ArgumentParser(
        description=f"BiliLiveCut {VERSION} — Portable Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    Launch:                  BiliLiveCut-Portable.exe
  Specify Engine Pack:      BiliLiveCut-Portable.exe --engine-pack ./BiliLiveCut-EnginePack.zip
    Offline:                 BiliLiveCut-Portable.exe --offline
    Verify models:           BiliLiveCut-Portable.exe --verify-models
    Doctor:                  BiliLiveCut-Portable.exe --doctor
  Repair damaged Runtime:             BiliLiveCut-Portable.exe --repair
    Version:                 BiliLiveCut-Portable.exe --version
""",
    )
    parser.add_argument(
        "--engine-pack", type=str, default=None, metavar="PATH", help="Specify local Engine Pack ZIP path"
    )
    parser.add_argument(
        "--offline", action="store_true", help="Offline mode: block network model download (local Engine Pack only)"
    )
    parser.add_argument(
        "--fallback-online",
        action="store_true",
        help="Allow online download when Engine Pack validation fails (only with --engine-pack mode)",
    )
    parser.add_argument("--verify-runtime", action="store_true", help="Verify installed Runtime integrity")
    parser.add_argument(
        "--verify-models", action="store_true", help="Verify installed model integrity (per-file SHA-256)"
    )
    parser.add_argument("--repair", action="store_true", help="Repair mode: reinstall Runtime and models")
    parser.add_argument("--doctor", action="store_true", help="Run System Diagnostics check")
    parser.add_argument("--version", action="store_true", help="Show version info and exit")
    return parser


def run_launcher(args: argparse.Namespace) -> int:
    """Execute launch flow。

    :param args: 解析后的命令行参数。
    :returns: 退出码 (0 成功, 非零失败)。
    """
    user_engine_pack_path = args.engine_pack

    app_root = get_app_root()
    os.chdir(str(app_root))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    print("=" * 60)
    print(f"  {APP_NAME} {VERSION} — Portable Launcher")
    print("=" * 60)
    print(f"  Working dir: {app_root}")
    print("  GitHub requests: 0 (source from built-in Payload)")
    print()

    try:
        # --engine-pack and --fallback-online validation
        if args.fallback_online and not args.engine_pack:
            print("[ERROR] --fallback-online requires --engine-pack PATH")
            return 1

        if args.engine_pack:
            ep_path = Path(args.engine_pack)
            if not ep_path.exists():
                print(f"[ERROR] Engine Pack not found: {ep_path}")
                return 1
            if not ep_path.is_file():
                print(f"[ERROR] Engine Pack path is not a file: {ep_path}")
                return 1

        # --doctor 模式
        if args.doctor:
            return _run_doctor(app_root)

        # --verify-runtime 模式
        if args.verify_runtime:
            from blc_portable.runtime.verifier import verify_runtime

            ok, errors = verify_runtime(app_root)
            if errors:
                for e in errors:
                    print(f"  [FAIL] {e}")
            if ok:
                print("\n  [PASS] Runtime verification: PASS")
            else:
                print("\n  [FAIL] Runtime verification: FAIL")
            return 0 if ok else 1

        # --verify-models 模式
        if args.verify_models:
            _verify_installed_models(app_root)
            return 0

        # --offline 模式: blocking network requests
        if args.offline:
            os.environ["BLC_OFFLINE"] = "1"
            os.environ["PIP_NO_INDEX"] = "1"
            print("  [OFFLINE] Offline mode enabled -- all network requests blocked")

        # --repair 模式
        if args.repair:
            _repair_runtime(app_root)
            print()

        # 1. ensure persistent data dirs
        ensure_data_dirs(app_root)

        # 2. check/install Runtime
        source_dir = get_current_release_dir()
        if source_dir is None:
            print("[1/6] Installing source from built-in Payload...")
            source_dir = install_source_from_payload(app_root)
            print()
        else:
            print(f"[1/6] Runtime ready: {source_dir}")
            print()

        # 3. 确保 .env
        print("[2/6] Config file...")
        ensure_env(app_root, source_dir)
        print()

        # 4. Prepare virtual environment
        print("[3/6] Python environment...")
        venv_python = prepare_venv(app_root)
        print()

        # 5. Dependency install (from ABI-specific lock file)
        print("[4/6] Dependency install...")
        install_dependencies(venv_python, app_root, source_dir=source_dir)
        print()

        # 6. 模型准备
        print("[5/6] Model preparation...")
        model_result = prepare_models(
            venv_python,
            app_root,
            user_engine_pack_path,
            offline=args.offline,
            fallback_online=args.fallback_online,
        )
        model_source = model_result.get("source", "unknown")
        network_reqs = model_result.get("network_requests", 0)
        print(f"  Model source: {model_source} (network requests: {network_reqs})")
        print()

        # 7. 启动 Web
        print("[6/6] Starting Web console...")
        print()
        print("=" * 60)
        print("  Browser will open: http://127.0.0.1:8000")
        print("  Press Ctrl+C to stop")
        print("=" * 60)
        print()

        env = os.environ.copy()
        bin_dir = app_root / "bin"
        if (bin_dir / "ffmpeg.exe").exists():
            env["FFMPEG_PATH"] = str(bin_dir / "ffmpeg.exe")
            env["FFPROBE_PATH"] = str(bin_dir / "ffprobe.exe")
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

        env["BLC_PORTABLE"] = "1"
        env["BLC_SOURCE_DIR"] = str(source_dir)
        env["PYTHONPATH"] = str(source_dir)

        # 清除代理环境变量：Portable 模式仅需服务 localhost，httpx 不需要 SOCKS
        for proxy_var in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "http_proxy", "https_proxy"):
            env.pop(proxy_var, None)

        models_dir = app_root / "models"
        if models_dir.exists():
            env["BLC_MODELS_DIR"] = str(models_dir)

        result = subprocess.run(_build_service_command(venv_python), env=env, cwd=str(app_root))
        return result.returncode

    except KeyboardInterrupt:
        print("\nService stopped")
        return 0
    except Exception:
        print("\nService exited with error:")
        traceback.print_exc()
        print()
        _pause_before_exit()
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    """主入口 — importable and testable callable entrypoint。

    :param argv: 命令行参数列表 (None 使用 sys.argv)。
    :returns: 退出码 (0 成功, 非零失败)。
    """
    configure_console_encoding()
    parser = build_parser()

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse 内置 help/error 处理already printed message
        return int(str(e)) if str(e) else 0

    # --version separate handling (不进入 run_launcher 的heavy startup flow)
    if args.version:
        print(f"BiliLiveCut Portable {VERSION}")
        print(f"Release Version: {RELEASE_VERSION}")
        print(f"Source Commit: {SOURCE_COMMIT_SHORT}")
        try:
            manifest = get_payload_manifest()
            print(f"Payload SHA256: {manifest.get('payload_sha256', 'N/A')[:32]}")
        except RuntimeError:
            print("Payload SHA256: (unreadable)")
        pack_info = get_engine_pack_info()
        if pack_info:
            print(f"Engine Pack: {pack_info.get('engine_pack_version', 'N/A')} CRC32={pack_info.get('crc32', 'N/A')}")
        return 0

    return run_launcher(args)


if __name__ == "__main__":
    raise SystemExit(main())
