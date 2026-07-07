"""将 launcher.py 编译为单个 launcher.exe。

用法:
    python build_exe.py            # 编译
    python build_exe.py --clean    # 编译前清理临时文件
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LAUNCHER_PY = ROOT / "launcher.py"
OUTPUT = ROOT / "launcher.exe"


def build(clean: bool = False) -> None:
    """编译 launcher.py -> launcher.exe(使用 PyInstaller --onefile)。"""
    if not LAUNCHER_PY.exists():
        print(f"[错误] 未找到 {LAUNCHER_PY}")
        sys.exit(1)

    if clean:
        for d in ("build", "__pycache__"):
            p = ROOT / d
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
        spec = ROOT / "launcher.spec"
        if spec.exists():
            spec.unlink()

    print("=" * 60)
    print("  BiliLiveCut 启动器编译")
    print(f"  源文件: {LAUNCHER_PY}")
    print(f"  输出:   {OUTPUT}")
    print("=" * 60)
    print()

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--console",
        "--name",
        "launcher",
        "--distpath",
        str(ROOT),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT),
        "--clean",
        str(LAUNCHER_PY),
    ]

    print(f"  执行: {' '.join(cmd)}")
    print()

    result = subprocess.run(cmd, cwd=str(ROOT))

    if result.returncode != 0:
        print(f"\n[错误] PyInstaller 编译失败(退出码 {result.returncode})。")
        sys.exit(1)

    # 清理 PyInstaller 产生的 .spec 和 build 目录(可选,源码保留在 launcher.py)
    spec = ROOT / "launcher.spec"
    if spec.exists():
        spec.unlink()
    build_dir = ROOT / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)

    # 验证产物
    if OUTPUT.exists():
        size = OUTPUT.stat().st_size / (1024 * 1024)
        print()
        print(f"  [OK] 编译成功: {OUTPUT} ({size:.1f} MB)")
        print("  将此文件放在 packaging/portable/ 目录下,双击即可启动。")
    else:
        print("\n[错误] 未生成 launcher.exe,编译可能失败。")
        sys.exit(1)


if __name__ == "__main__":
    clean = "--clean" in sys.argv
    build(clean=clean)
