#!/usr/bin/env python3
"""Build reproducible pure-Python wheels missing from the Windows wheel index."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SOURCE_DATE_EPOCH = "1700000000"
REQUIRED_BUILD_TOOLS = {"setuptools": "83.0.0", "wheel": "0.46.3"}


@dataclass(frozen=True)
class SourceWheel:
    """Pinned source archive and its reproducible wheel output."""

    filename: str
    url: str
    source_sha256: str
    wheel_filename: str
    wheel_sha256: str | None = None
    force_pure_python: bool = False


SOURCE_WHEELS = (
    SourceWheel(
        filename="jieba-0.42.1.tar.gz",
        url="https://files.pythonhosted.org/packages/source/j/jieba/jieba-0.42.1.tar.gz",
        source_sha256="055ca12f62674fafed09427f176506079bc135638a14e23e25be909131928db2",
        wheel_filename="jieba-0.42.1-py3-none-any.whl",
        wheel_sha256="f635d5b81df159e91c81ea7179cf2dd47631e77b62a11690553ea6973804f776",
    ),
    SourceWheel(
        filename="oss2-2.19.1.tar.gz",
        url="https://files.pythonhosted.org/packages/source/o/oss2/oss2-2.19.1.tar.gz",
        source_sha256="a8ab9ee7eb99e88a7e1382edc6ea641d219d585a7e074e3776e9dec9473e59c1",
        wheel_filename="oss2-2.19.1-py3-none-any.whl",
        wheel_sha256="c91a90140f9769e60149db7f54f7069778a96aaec0c7dc6c23d4ae2f5eeb8da7",
    ),
    SourceWheel(
        filename="crcmod-1.7.tar.gz",
        url="https://files.pythonhosted.org/packages/source/c/crcmod/crcmod-1.7.tar.gz",
        source_sha256="dc7051a0db5f2bd48665a990d3ec1cc305a466a77358ca4492826f41f283601e",
        wheel_filename="crcmod-1.7-py3-none-any.whl",
        wheel_sha256="a5c73de3c773b72cc842002639640626e4511c51e122056111a732f398af3931",
        force_pure_python=True,
    ),
    SourceWheel(
        filename="aliyun-python-sdk-core-2.16.0.tar.gz",
        url=(
            "https://files.pythonhosted.org/packages/source/a/aliyun-python-sdk-core/"
            "aliyun-python-sdk-core-2.16.0.tar.gz"
        ),
        source_sha256="651caad597eb39d4fad6cf85133dffe92837d53bdf62db9d8f37dab6508bb8f9",
        wheel_filename="aliyun_python_sdk_core-2.16.0-py3-none-any.whl",
        wheel_sha256="162622ba710232b3031026653064f99d0f4b2622b5c95025a51e17ae6d5b085c",
    ),
    SourceWheel(
        filename="antlr4-python3-runtime-4.9.3.tar.gz",
        url=(
            "https://files.pythonhosted.org/packages/source/a/antlr4-python3-runtime/"
            "antlr4-python3-runtime-4.9.3.tar.gz"
        ),
        source_sha256="f224469b4168294902bb1efa80a8bf7855f24c99aef99cbefc1bcd3cce77881b",
        wheel_filename="antlr4_python3_runtime-4.9.3-py3-none-any.whl",
        wheel_sha256="fb1e0eb0ba2a8694f69ab22c5ecad4a9b535c80d4511cd5aba19c17c021e1cc6",
    ),
)


def sha256_file(path: Path) -> str:
    """Return a lowercase SHA-256 digest for a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_build_tools() -> None:
    """Reject unpinned build tooling because it changes wheel metadata."""
    if sys.platform != "win32":
        raise RuntimeError("Reproducible runtime wheels must be built on Windows")
    mismatches = []
    for package, expected in REQUIRED_BUILD_TOOLS.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual = "not installed"
        if actual != expected:
            mismatches.append(f"{package}=={expected} required, found {actual}")
    if mismatches:
        raise RuntimeError("Unpinned wheel build environment:\n  " + "\n  ".join(mismatches))


def download_verified(package: SourceWheel, cache_dir: Path) -> Path:
    """Download one pinned sdist and verify its digest before extraction."""
    destination = cache_dir / package.filename
    if not destination.exists() or sha256_file(destination) != package.source_sha256:
        destination.unlink(missing_ok=True)
        print(f"Downloading {package.filename}")
        with urllib.request.urlopen(package.url, timeout=120) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)

    actual = sha256_file(destination)
    if actual != package.source_sha256:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"Source hash mismatch for {package.filename}: expected {package.source_sha256}, got {actual}"
        )
    return destination


def extract_safely(archive: Path, destination: Path) -> Path:
    """Extract a source archive after rejecting links and path traversal."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"Unsafe path in {archive.name}: {member.name}")
            if member.issym() or member.islnk():
                raise RuntimeError(f"Links are not allowed in {archive.name}: {member.name}")
        tar.extractall(destination, members=members, filter="data")

    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise RuntimeError(f"Expected one source root in {archive.name}, found {len(roots)}")
    return roots[0]


def force_crcmod_pure_python(source_root: Path) -> None:
    """Disable crcmod's optional C extension so one wheel supports py311/py312."""
    setup_py = source_root / "setup.py"
    content = setup_py.read_text(encoding="utf-8")
    extension_block = (
        "ext_modules=[ \n    Extension('crcmod._crcfunext', [os.path.join(base_dir,'src/_crcfunext.c'), ],\n    ),\n],"
    )
    if content.count(extension_block) != 1:
        raise RuntimeError("crcmod setup.py no longer matches the audited pure-Python patch")
    setup_py.write_text(content.replace(extension_block, "ext_modules=[],"), encoding="utf-8", newline="\n")


def build_wheel(package: SourceWheel, archive: Path, output_dir: Path, work_dir: Path) -> Path:
    """Build and verify one deterministic wheel."""
    source_root = extract_safely(archive, work_dir / package.filename.removesuffix(".tar.gz"))
    if package.force_pure_python:
        force_crcmod_pure_python(source_root)

    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
    env["PYTHONHASHSEED"] = "0"
    result = subprocess.run(
        [sys.executable, "setup.py", "bdist_wheel", "--dist-dir", str(output_dir.resolve())],
        cwd=source_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        output = (result.stdout + "\n" + result.stderr).strip()
        raise RuntimeError(f"Wheel build failed for {package.filename}:\n{output}")

    wheel = output_dir / package.wheel_filename
    if not wheel.is_file():
        produced = ", ".join(path.name for path in sorted(output_dir.glob("*.whl")))
        raise RuntimeError(f"Expected {package.wheel_filename}; wheel directory contains: {produced}")
    actual = sha256_file(wheel)
    if package.wheel_sha256 is not None and actual != package.wheel_sha256:
        wheel.unlink(missing_ok=True)
        raise RuntimeError(
            f"Wheel hash mismatch for {package.wheel_filename}: expected {package.wheel_sha256}, got {actual}"
        )
    print(f"Built {wheel.name} sha256={actual}")
    return wheel


def build_all(output_dir: Path, cache_dir: Path) -> list[Path]:
    """Build all pinned source-only wheels."""
    verify_build_tools()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    expected_names = {package.wheel_filename for package in SOURCE_WHEELS}
    for existing in output_dir.glob("*.whl"):
        if existing.name in expected_names:
            existing.unlink()

    with tempfile.TemporaryDirectory(prefix="blc-runtime-wheels-") as temporary:
        work_dir = Path(temporary)
        return [
            build_wheel(package, download_verified(package, cache_dir), output_dir, work_dir)
            for package in SOURCE_WHEELS
        ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/portable-runtime-sdists"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Build all source-only runtime wheels."""
    args = parse_args(argv)
    wheels = build_all(args.output_dir.resolve(), args.cache_dir.resolve())
    print(f"Built and verified {len(wheels)} source-only wheels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
