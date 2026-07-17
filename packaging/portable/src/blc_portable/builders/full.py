"""Portable Full bundle build script.

Output: BiliLiveCut-Portable-Full-v{version}-x64.zip
Contents: EXE + Portable Python + offline Wheels + FFmpeg (no models)

Note: Models are NOT bundled in Full. Four ASR engine models are provided
by a separate Engine Pack.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

PORTABLE_DIR = Path(__file__).resolve().parent.parent.parent.parent
PROJECT_ROOT = PORTABLE_DIR.parent.parent
DIST_DIR = PORTABLE_DIR / "dist" / "full"
LITE_DIR = PORTABLE_DIR / "dist" / "lite"
PAYLOAD_DIR = PORTABLE_DIR / "dist" / "payload"
RESOURCES_DIR = PORTABLE_DIR / "resources"

RELEASE_VERSION = "0.1.14.11-alpha"
FULL_NAME = f"BiliLiveCut-Portable-Full-{RELEASE_VERSION}-x64"


def build_full_bundle() -> Path:
    """Build Portable Full offline bundle.

    Full bundle must contain:
    - BiliLiveCut-Portable.exe (Lite EXE)
    - portable-python/ (embedded Python runtime)
    - vendor/wheels/ (offline dependency packages)
    - bin/ffmpeg.exe, bin/ffprobe.exe
    - README.txt, checksums.json, SHA256SUMS.txt

    Note: Full does NOT include engine models. Models are provided
    by a separate Engine Pack.

    :returns: Path to generated ZIP file.
    :raises RuntimeError: When critical components are missing.
    """
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  BiliLiveCut Portable Full {RELEASE_VERSION}")
    print("=" * 60)

    # 确保 Lite EXE 存在
    lite_exe_name = f"BiliLiveCut-Portable-Lite-v{RELEASE_VERSION}-x64.exe"
    lite_path = LITE_DIR / lite_exe_name
    if not lite_path.exists():
        raise RuntimeError(f"Lite EXE not found: {lite_path}\nBuild it first: python build_exe.py")
    print(f"  EXE: {lite_path.stat().st_size / 1024 / 1024:.1f} MB")

    # 验证必须的离线组件
    missing: list[str] = []

    portable_py = PROJECT_ROOT / "portable-python" / "python.exe"
    if not portable_py.exists():
        missing.append("portable-python/python.exe (prepare Portable Python first)")

    wheels_dir = PROJECT_ROOT / "vendor" / "wheels"
    if not wheels_dir.exists() or not list(wheels_dir.glob("*.whl")):
        candidate = PROJECT_ROOT / "vendor" / "wheels"
        if candidate.exists() and list(candidate.glob("*.whl")):
            wheels_dir = candidate
        else:
            missing.append("vendor/wheels/ (no .whl files -- offline install will not work)")

    ffmpeg = PROJECT_ROOT / "bin" / "ffmpeg.exe"
    if not ffmpeg.exists():
        candidate = PROJECT_ROOT / "bin" / "ffmpeg.exe"
        if candidate.exists():
            ffmpeg = candidate
        else:
            missing.append("bin/ffmpeg.exe")

    ffprobe = PROJECT_ROOT / "bin" / "ffprobe.exe"
    if not ffprobe.exists():
        candidate = PROJECT_ROOT / "bin" / "ffprobe.exe"
        if candidate.exists():
            ffprobe = candidate
        else:
            missing.append("bin/ffprobe.exe")

    if missing:
        is_fixture = os.environ.get("BLC_FIXTURE_BUILD") == "1" or os.environ.get("BLC_CI_BUILD") == "1"
        if is_fixture:
            print("\n  [WARNING] Full offline bundle is missing (fixture mode, continuing):")
            for m in missing:
                print(f"    - {m}")
        else:
            raise RuntimeError(
                "Full build FAILED — missing components:\n  "
                + "\n  ".join(missing)
                + "\nFull offline bundle prerequisites:"
                + "\n  1. portable-python/ directory (Python 3.11/3.12)"
                + "\n  2. vendor/wheels/ directory (offline Wheels)"
                + "\n  3. bin/ffmpeg.exe and bin/ffprobe.exe"
            )

    # 读取 Engine Pack 信息 (如有)
    engine_pack_crc32 = ""
    ep_info_path = RESOURCES_DIR / "engine_pack_info.json"
    if ep_info_path.exists():
        ep_info = json.loads(ep_info_path.read_text(encoding="utf-8"))
        engine_pack_crc32 = ep_info.get("crc32", "")
        print(f"  Engine Pack CRC32: {engine_pack_crc32 or '(empty)'}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bundle = tmp / FULL_NAME
        bundle.mkdir(parents=True)

        # 复制 EXE
        shutil.copy2(lite_path, bundle / "BiliLiveCut-Portable.exe")
        print("  [OK] Copied EXE")

        # 复制 portable-python (如果存在)
        pp_src = None
        for candidate in [
            PROJECT_ROOT / "portable-python",
            Path("portable-python"),
        ]:
            if candidate.exists() and (candidate / "python.exe").exists():
                pp_src = candidate
                break
        if pp_src:
            pp_dst = bundle / "portable-python"
            shutil.copytree(pp_src, pp_dst, dirs_exist_ok=True)
            py_count = sum(1 for _ in pp_dst.rglob("*.exe") if _.is_file())
            print(f"  [OK] Copied portable-python ({py_count} executables)")
        else:
            print("  [SKIP] portable-python not found")

        # 复制 vendor/wheels (如果存在)
        wh_src = None
        for candidate in [
            PROJECT_ROOT / "vendor" / "wheels",
            Path("vendor") / "wheels",
        ]:
            if candidate.exists() and list(candidate.glob("*.whl")):
                wh_src = candidate
                break
        if wh_src:
            wh_dst = bundle / "vendor" / "wheels"
            wh_dst.mkdir(parents=True, exist_ok=True)
            wh_count = 0
            for whl in wh_src.glob("*.whl"):
                shutil.copy2(whl, wh_dst / whl.name)
                wh_count += 1
            print(f"  [OK] Copied {wh_count} wheels")
        else:
            print("  [SKIP] vendor/wheels not found")

        # 复制 FFmpeg (如果存在)
        for tool_name in ("ffmpeg.exe", "ffprobe.exe"):
            tool_src = None
            for candidate in [
                PROJECT_ROOT / "bin" / tool_name,
                Path("bin") / tool_name,
            ]:
                if candidate.exists():
                    tool_src = candidate
                    break
            if tool_src:
                tool_dst = bundle / "bin"
                tool_dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(tool_src, tool_dst / tool_name)
                print(f"  [OK] Copied {tool_name}")
            else:
                print(f"  [SKIP] {tool_name} not found")

        # README
        import datetime

        readme = bundle / "README.txt"
        readme.write_text(
            f"""\
BiliLiveCut Portable Full {RELEASE_VERSION}
============================================

Complete launch package -- no extra downloads during installation.

Compared to Lite:
  - Built-in Portable Python (no system Python required)
  - Built-in offline Wheels (no network for dependency install)
  - Built-in FFmpeg/FFprobe (no system FFmpeg required)

No models included:
  - Four ASR engine models are provided by a separate Engine Pack.
  - Place BiliLiveCut-EnginePack-{RELEASE_VERSION}.zip in the same directory;
    CRC32 is verified and models installed on first launch.
  - Without Engine Pack, all four engine models are downloaded online.

File structure:
  BiliLiveCut-Portable.exe    # Launcher (double-click to run)
  portable-python/            # Embedded Python runtime
  vendor/wheels/              # Offline dependency packages
  bin/                        # FFmpeg/FFprobe

First launch:
  Double-click BiliLiveCut-Portable.exe
  -> Extract source code from built-in Payload
  -> Detect portable-python
  -> Install dependencies (--no-index, local wheels)
  -> Detect Engine Pack or download models online
  -> Start web console

Data directories (generated after run):
  data/       Database
  storage/    Recording files and clips
  models/     Four ASR engine models (installed from Engine Pack or online)
  logs/       Log files
  .env        Configuration file

Source:
  Source baseline commit: [see checksums.json]
  Release version: {RELEASE_VERSION}
  Build time: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
""",
            encoding="utf-8",
        )

        # checksums.json
        manifest_path = PAYLOAD_DIR / "payload_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        import hashlib

        exe_sha256 = hashlib.sha256()
        with open(lite_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                exe_sha256.update(chunk)

        checksums = {
            "release_version": RELEASE_VERSION,
            "source_commit": manifest["source_commit"],
            "exe_sha256": exe_sha256.hexdigest(),
            "engine_pack_crc32": engine_pack_crc32,
        }
        (bundle / "checksums.json").write_text(json.dumps(checksums, ensure_ascii=False, indent=2), encoding="utf-8")

        # 打包 ZIP
        zip_path = DIST_DIR / f"{FULL_NAME}.zip"
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(bundle.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(tmp).as_posix()
                    zf.write(p, rel)

        # 生成 build-manifest.json 和 SHA256SUMS
        zip_sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        build_manifest = {
            "release_version": RELEASE_VERSION,
            "source_commit": manifest["source_commit"],
            "source_commit_short": manifest["source_commit_short"],
            "builder_commit": manifest["builder_commit"],
            "architecture": "x64",
            "artifact_type": "full",
            "payload_sha256": manifest["payload_sha256"],
            "artifact_sha256": zip_sha256,
            "engine_pack_crc32": engine_pack_crc32,
        }

        (DIST_DIR / "build-manifest.json").write_text(
            json.dumps(build_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (DIST_DIR / "SHA256SUMS.txt").write_text(f"{zip_sha256}  {FULL_NAME}.zip\n", encoding="utf-8")

        print(f"\n  [OK] {zip_path.name} ({zip_path.stat().st_size / 1024 / 1024:.1f} MB)")
        print(f"  SHA256: {zip_sha256[:32]}")

    return zip_path


def main() -> int:
    """Entry point -- called by thin wrapper.

    :returns: 0 on success, 1 on failure.
    """
    try:
        build_full_bundle()
        return 0
    except SystemExit as e:
        return int(str(e)) if str(e) else 0
    except Exception as exc:
        try:
            print(f"[Error] {exc}")
        except UnicodeEncodeError:
            print(f"[Error] {exc!r}")
        return 1


if __name__ == "__main__":
    build_full_bundle()
