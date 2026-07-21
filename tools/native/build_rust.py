"""BiliLiveCut Rust 加速模块编译脚本.

编译 O(N**2) 聚类矩阵 Rust 扩展 (_rust_cluster.pyd/.so)。

用法:
    python tools/native/build_rust.py          # 编译并复制到包目录
    python tools/native/build_rust.py --check  # 仅检查 Rust 环境是否可用

可以从仓库根目录或任何工作目录运行 — 脚本自动定位仓库根目录。

前置条件:
    - Rust 工具链 (https://rustup.rs): rustc + cargo
    - Windows: Visual Studio Build Tools (C++ 桌面开发)
    - Linux/Mac: gcc/clang
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# tools/native/ → 仓库根目录
_REPO_ROOT = _HERE.parent.parent
RUST_SRC = _REPO_ROOT / "tools" / "native" / "rust"
TARGET_DIR = _REPO_ROOT / "app" / "analysis"


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """运行命令并打印输出。

    :param cmd: 命令行。
    :param cwd: 工作目录。
    :returns: 子进程结果。
    """
    print(f"  [build_rust] {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def check_rust() -> bool:
    """检查 Rust/cargo 是否可用。

    :returns: True 如果可用。
    """
    try:
        r = subprocess.run(
            ["cargo", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if r.returncode == 0:
            print(f"  [build_rust] Rust 环境: {r.stdout.strip()}")
            return True
        print(f"  [build_rust] cargo 返回码非零: {r.stderr.strip()}")
        return False
    except FileNotFoundError:
        print("  [build_rust] 未检测到 cargo 命令。安装 Rust: https://rustup.rs")
        return False
    except Exception as exc:
        print(f"  [build_rust] 检测 cargo 异常: {exc}")
        return False


def build() -> bool:
    """编译 Rust 扩展并复制到目标目录。

    :returns: True 如果编译成功。
    """
    if not RUST_SRC.is_dir():
        print(f"  [build_rust] 错误: 源码目录不存在 {RUST_SRC}")
        return False

    if not (RUST_SRC / "Cargo.toml").exists():
        print("  [build_rust] 错误: 缺少 Cargo.toml")
        return False

    print(f"  [build_rust] 编译 Rust 扩展 ({RUST_SRC})…")

    # Python 3.14 兼容性（pyo3 0.22.6 官方支持到 3.13）
    import os as _os

    env = _os.environ.copy()
    env.setdefault("PYO3_USE_ABI3_FORWARD_COMPATIBILITY", "1")
    env.setdefault("PYO3_PYTHON", sys.executable)

    # cargo build --release (带 Python 3.14 兼容性)
    import subprocess as _sp

    result = _sp.run(
        ["cargo", "build", "--release"],
        cwd=RUST_SRC,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if result.returncode != 0:
        print(f"  [build_rust] 编译失败:\n{result.stderr}")
        return False

    print(f"  [build_rust] 编译成功\n{result.stdout.strip()}")

    # 查找产物 (.pyd, .so, .dll)
    ext = ".pyd" if "win" in sys.platform else ".so"
    build_dir = RUST_SRC / "target" / "release"
    candidates = list(build_dir.glob(f"_rust_cluster*{ext}"))
    # 也匹配 .dll (Windows Rust 默认输出)
    if sys.platform == "win32" and not candidates:
        candidates = list(build_dir.glob("_rust_cluster*.dll"))
    # macOS: lib_rust_cluster.dylib
    if not candidates:
        candidates = list(build_dir.glob("lib_rust_cluster*"))

    if not candidates:
        print(f"  [build_rust] 错误: 未找到编译产物 ({ext})")
        return False

    src = candidates[0]
    dst = TARGET_DIR / f"_rust_cluster{ext}"
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"  [build_rust] 复制: {src.name} → {dst}")
    print("  [build_rust] Rust 扩展已就绪 [OK]")
    return True


def main() -> int:
    """入口。

    :returns: 0 = 成功, 1 = 失败。
    """
    print("=" * 60)
    print("  BiliLiveCut Rust 加速模块编译")
    print(f"  仓库根目录: {_REPO_ROOT}")
    print("=" * 60)

    if "--check" in sys.argv:
        return 0 if check_rust() else 1

    if not check_rust():
        print("\n  [build_rust] 提示: 无 Rust 环境,跳过编译。")
        print("  项目将自动使用纯 Python 回退 (_speedups_round2_py)。")
        return 1

    if not build():
        return 1

    print("\n  [build_rust] 完成。启动项目即可享受 Rust 加速。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
