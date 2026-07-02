"""BiliLiveCut 即插即用启动器(将被编译为单个 .exe)。

----
将此 .exe 放在 Public/ 目录下双击运行,自动完成:
1. 检测系统 Python 环境
2. 创建虚拟环境(如需)
3. 安装依赖(使用 vendor/wheels 离线安装,无需联网)
4. 验证模型与 ffmpeg 就绪
5. 启动 Web 管理后台
6. 自动打开浏览器访问控制台(http://127.0.0.1:8000)

无需 .ps1 / .bat,无安全策略拦截问题;依赖全部从包内 vendor/wheels 离线安装。
----
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
import webbrowser
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────────
VENV_DIR = ".venv"
WHEELS_DIR = "vendor" + os.sep + "wheels"
REQUIREMENTS = "requirements-bundle.txt"

# ── 辅助函数 ──────────────────────────────────────────────────────


def _human_size(size: int) -> str:
    """字节数转可读格式。"""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _find_system_python() -> Path | None:
    """查找系统 Python 3.11+(不包含 .venv 内)。"""
    candidates = ["python", "python3", "py"]
    for name in candidates:
        try:
            result = subprocess.run(
                [name, "-c", "import sys; print(sys.version_info[:2])"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            v = tuple(int(x) for x in result.stdout.strip().strip("()").split(","))
            if v >= (3, 11):
                # 找到后把它转成绝对路径,避免 PyInstaller 打包后 PATH 变化
                full = subprocess.run(
                    ["where", name] if sys.platform == "win32" else ["which", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                lines = full.stdout.strip().splitlines()
                for line in lines:
                    p = Path(line.strip())
                    if p.exists():
                        return p
        except Exception:
            continue
    return None


def _fail(msg: str) -> None:
    """输出错误消息并暂停,等待用户按键后退出。"""
    print()
    print("*" * 60)
    print(f"  [错误] {msg}")
    print("*" * 60)
    print()
    print("按 Enter 键退出...")
    input()
    sys.exit(1)


# ── 主流程 ────────────────────────────────────────────────────────


def main() -> None:
    root = Path(sys.argv[0]).resolve().parent
    os.chdir(str(root))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    print("=" * 60)
    print("  BiliLiveCut V0.1.0 Alpha — 即插即用启动器")
    print("=" * 60)
    print(f"  工作目录: {root}")
    print()

    # ================================================================
    # 1) 检测 Python
    # ================================================================
    venv_python = root / VENV_DIR / "Scripts" / "python.exe"
    if not venv_python.exists():
        system_py = _find_system_python()
        if system_py is None:
            _fail(
                "未找到 Python 3.11+。\n"
                "请从 https://www.python.org/downloads/ 下载并安装 Python 3.11 或更高版本,\n"
                "安装时勾选「Add Python to PATH」。"
            )
        print("[1/5] 系统 Python:")
        try:
            ver = subprocess.check_output(
                [str(system_py), "--version"], text=True, timeout=10
            ).strip()
        except Exception:
            ver = "(未知)"
        print(f"       {ver}  ({system_py})")
    else:
        system_py = venv_python
        print("[1/5] Python: 虚拟环境已存在")
    print()

    # ================================================================
    # 2-3) 创建虚拟环境 + 安装依赖(如需要)
    # ================================================================
    if not venv_python.exists():
        print("[2/5] 创建虚拟环境 .venv ...")
        try:
            subprocess.run(
                [str(system_py), "-m", "venv", VENV_DIR],
                check=True,
                timeout=120,
            )
            print("       ✓ 完成")
        except subprocess.CalledProcessError:
            _fail("创建虚拟环境失败。请确认磁盘空间充足、Python 安装完整。")
        print()

        wheels = root / WHEELS_DIR
        if not wheels.exists() or not list(wheels.glob("*.whl")):
            _fail(
                f"未找到依赖 wheel 目录: {wheels}\n"
                "请确保 vendor/wheels/ 中存在 .whl 文件(在联网机器执行 build_bundle.py 生成)。"
            )
        print("[3/5] 离线安装依赖(vendor/wheels)...")
        try:
            subprocess.run(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links",
                    str(wheels),
                    "-r",
                    REQUIREMENTS,
                ],
                check=True,
                timeout=600,
            )
            print("       ✓ 完成")
        except subprocess.CalledProcessError:
            _fail("离线安装依赖失败。请确认 vendor/wheels/ 中 wheel 齐全且平台一致。")
        print()
    else:
        print("[2/5] 虚拟环境已存在,跳过创建。")
        # 快速验证关键包可导入(不做完整 pip check)。
        try:
            subprocess.run(
                [
                    str(venv_python),
                    "-c",
                    "import faster_whisper, fastapi, uvicorn, sqlmodel; print('ok')",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            print("       ✓ 依赖校验通过")
        except subprocess.CalledProcessError:
            print("       ⚠ 依赖校验未通过,尝试重新安装 ...")
            wheels = root / WHEELS_DIR
            subprocess.run(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links",
                    str(wheels),
                    "-r",
                    REQUIREMENTS,
                ],
                check=True,
                timeout=600,
            )
        print("[3/5] 依赖就绪")
        print()

    # ================================================================
    # 4) 验证模型与 ffmpeg
    # ================================================================
    model_bin = root / "models" / "whisper-large-v3-turbo" / "model.bin"
    if not model_bin.exists():
        _fail(
            "未找到 Whisper 模型(models/whisper-large-v3-turbo/model.bin)。\n"
            "请在联网机器执行 build_bundle.py 下载模型,或将模型放入此目录。"
        )
    print(f"[4/5] Whisper 模型: {_human_size(model_bin.stat().st_size)}")

    bin_dir = root / "bin"
    if (bin_dir / "ffmpeg.exe").exists():
        print(f"       FFmpeg: 包内({_human_size((bin_dir / 'ffmpeg.exe').stat().st_size)})")
    else:
        print("       FFmpeg: 使用系统 PATH(包内未找到)")
    print()

    # ================================================================
    # 5) 启动服务
    # ================================================================
    print("[5/5] 启动 Web 控制台...")
    print()
    print(f"  ▸ 浏览器将自动打开: http://127.0.0.1:8000")
    print(f"  ▸ 按 Ctrl+C 停止服务。")
    print()

    env = os.environ.copy()
    env["WHISPER_MODEL"] = str(root / "models" / "whisper-large-v3-turbo")
    if (bin_dir / "ffmpeg.exe").exists():
        env["FFMPEG_PATH"] = str(bin_dir / "ffmpeg.exe")
        env["FFPROBE_PATH"] = str(bin_dir / "ffprobe.exe")
        env["PATH"] = str(bin_dir) + ";" + env.get("PATH", "")

    # 先开浏览器再启服务(避免服务阻塞后浏览器不弹)
    webbrowser.open("http://127.0.0.1:8000")

    try:
        subprocess.run(
            [
                str(venv_python),
                "-m",
                "app.cli",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ],
            env=env,
            cwd=str(root),
        )
    except KeyboardInterrupt:
        print("\n服务已停止。")
    except Exception:
        print("\n服务异常退出:")
        traceback.print_exc()
        print("\n按 Enter 键退出...")
        input()


if __name__ == "__main__":
    main()
