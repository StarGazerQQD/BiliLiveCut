"""BiliLiveCut 即插即用启动器（将被编译为单个 .exe）。

----
双击此 .exe 即可全自动部署运行,无需拷贝任何其他文件:

1. 从 GitHub 下载源码（app/config/pyproject.toml/requirements-bundle.txt）
2. 检测/创建 Python 虚拟环境
3. 联网安装依赖（清华+阿里云镜像,国内极速）
4. 下载 Whisper large-v3-turbo 模型（hf-mirror.com,约 1.6GB）
5. 下载 ffmpeg/ffprobe（BtbN 静态构建,约 80MB）
6. 生成 .env 配置文件
7. 启动 Web 管理后台,自动打开浏览器

已有组件自动跳过,断点续跑;零文件拷贝,一个 .exe 走天下。
----
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

# ── 常量 ──────────────────────────────────────────────────────────
VENV_DIR = ".venv"
WHEELS_DIR = "vendor" + os.sep + "wheels"
REQUIREMENTS = "requirements-bundle.txt"
APP_NAME = "BiliLiveCut"
VERSION = "V0.1.12.6 Alpha"

# GitHub 源码归档（公共仓库无需 token）
GITHUB_REPO = "StarGazerQQD/BiliLiveCut"
ARCHIVE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/zipball/main"
# GitHub 在境内常不稳,提供代理镜像作为回退
GH_MIRRORS = [
    ARCHIVE_URL,
    f"https://gh-proxy.com/{ARCHIVE_URL}",
    f"https://ghproxy.net/{ARCHIVE_URL}",
]

# Whisper 模型
MODEL_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
MODEL_DIRNAME = "whisper-large-v3-turbo"
HF_MIRROR = "https://hf-mirror.com"

# FFmpeg 静态构建（Windows x64）
FFMPEG_WIN_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)

# pip 镜像（国内极速）: 阿里云优先、清华备用;环境变量 PIP_INDEX_URL/PIP_EXTRA_INDEX_URL 可覆盖。
PIP_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
PIP_EXTRA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_TRUSTED_HOSTS = ["mirrors.aliyun.com", "pypi.tuna.tsinghua.edu.cn"]

DOWNLOAD_TIMEOUT_S = 60

# ── .env 模板（自展开时生成）──────────────────────────────────────
ENV_TEMPLATE = """\
# ===========================================================================
# BiliLiveCut 即插即用版配置（由启动器自动生成）
# ===========================================================================

# ---------- 通用 ----------
APP_ENV=prod
LOG_LEVEL=INFO

# ---------- pip 镜像（中国大陆加速;可通过环境变量 PIP_INDEX_URL / PIP_EXTRA_INDEX_URL 覆盖）----------
# 默认: 阿里云优先、清华备用
# PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
# PIP_EXTRA_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# ---------- 存储 ----------
STORAGE_ROOT=./storage
DATABASE_URL=sqlite:///./storage/blc.db

# ---------- FFmpeg（启动器会自动下载到 bin/ 或使用系统 PATH）----------
FFMPEG_PATH=ffmpeg
FFPROBE_PATH=ffprobe

# ---------- 录制 / 分片 ----------
SEGMENT_DURATION_S=60
PREFERRED_STREAM_PROTOCOL=hls
STREAM_QUALITY=10000
RECONNECT_MAX_BACKOFF_S=30
LIVE_POLL_INTERVAL_S=15
COLLECT_DANMAKU=true

# ---------- Bilibili 合规 ----------
REQUIRE_AUTHORIZATION=true
BILIBILI_COOKIE=

# ---------- AI: 语音转写 ----------
WHISPER_MODEL=./models/whisper-large-v3-turbo
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8

# ---------- AI: 大模型（可选;不填则走纯规则,零费用可用）----------
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_WEB_SEARCH_PARAM=enable_search
LLM_PRICE_INPUT_PER_M=0
LLM_PRICE_OUTPUT_PER_M=0
LLM_DAILY_BUDGET=0

# ---------- 网感资料库（可选;需配置趋势采集专用 API 或复用上面的 LLM）----------
TREND_ENABLED=false
TREND_API_KEY=
TREND_BASE_URL=
TREND_MODEL=
TREND_WEB_SEARCH=true
TREND_MAX_SEARCHES=5
TREND_MAX_ITEMS=40
TREND_RETENTION_DAYS=14
TREND_MATCH_DAYS=7

# ---------- 高光判断阈值 ----------
HIGHLIGHT_INIT_THRESHOLD=0.5
HIGHLIGHT_THRESHOLD=0.65
AUTO_PUBLISH_THRESHOLD=0.85

# ---------- 上传（默认 manual,零风险）----------
UPLOADER=manual
"""

# ── 辅助函数 ──────────────────────────────────────────────────────


def _human_size(size: int) -> str:
    """字节数转可读格式。"""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _find_system_python() -> Path | None:
    """查找系统 Python 3.11+（不包含 .venv 内）。"""
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
    for line in msg.strip().split("\n"):
        print(f"  [错误] {line}")
    print("*" * 60)
    print()
    print("按 Enter 键退出...")
    input()
    sys.exit(1)


def _step_header(step: str, root: Path) -> None:
    """打印步骤标题。"""
    print(f"[{step}]", flush=True)


def _pip_install(venv_python: Path, root: Path) -> None:
    """联网安装依赖（阿里云优先、清华备用;可通过 PIP_INDEX_URL / PIP_EXTRA_INDEX_URL 环境变量覆盖）。"""
    req = root / REQUIREMENTS
    index = os.environ.get("PIP_INDEX_URL", PIP_INDEX)
    extra = os.environ.get("PIP_EXTRA_INDEX_URL", PIP_EXTRA_INDEX)
    cmd = [
        str(venv_python), "-m", "pip", "install",
        "-r", str(req),
        "-i", index,
        "--extra-index-url", extra,
        *[f"--trusted-host={h}" for h in PIP_TRUSTED_HOSTS],
    ]
    subprocess.run(cmd, check=True, timeout=900)


def _has_source(root: Path) -> bool:
    """检查 app/ 源码是否已就位。"""
    return (root / "app" / "cli.py").exists() and (root / REQUIREMENTS).exists()


def _count_files(root: Path) -> int:
    """统计工作目录下文件总数（排除 .venv/ 和 __pycache__/）。"""
    n = 0
    for p in root.rglob("*"):
        if p.is_file() and ".venv" not in p.parts and "__pycache__" not in p.parts:
            n += 1
    return n


def _status_icon(ok: bool) -> str:
    """返回状态图标,[OK] 或 [---]."""
    return "[OK]  " if ok else "[---] "


def _scan_and_report(root: Path) -> None:
    """扫描目录状态并打印摘要:哪些已就绪,哪些需要下载。"""
    before = _count_files(root)
    source_ok = _has_source(root)
    venv_ok = (root / VENV_DIR / "Scripts" / "python.exe").exists()
    model_ok = (root / "models" / MODEL_DIRNAME / "model.bin").exists()
    ffmpeg_ok = (root / "bin" / "ffmpeg.exe").exists() or (root / "bin" / "ffmpeg").exists()
    env_ok = (root / ".env").exists()

    deps_ok = False
    if venv_ok:
        try:
            subprocess.run(
                [str(root / VENV_DIR / "Scripts" / "python.exe"), "-c",
                 "import faster_whisper, fastapi, uvicorn, sqlmodel"],
                check=True, capture_output=True, timeout=15,
            )
            deps_ok = True
        except Exception:
            pass

    all_ok = source_ok and venv_ok and deps_ok and model_ok and ffmpeg_ok and env_ok

    print(f"  目录文件数: {before}  状态: {'全部就绪' if all_ok else '部分缺失,将自动补充'}")
    print()
    print(f"   {_status_icon(source_ok)} 源码 (app/config/...)")
    print(f"   {_status_icon(venv_ok)}   虚拟环境 (.venv)")
    print(f"   {_status_icon(deps_ok)}   依赖 (pip 包)")
    print(f"   {_status_icon(model_ok)} Whisper 模型 (1.6 GB)")
    print(f"   {_status_icon(ffmpeg_ok)} FFmpeg (ffmpeg.exe)")
    print(f"   {_status_icon(env_ok)}   配置文件 (.env)")
    print()


# ── 步骤 1: 从 GitHub 下载源码 ──────────────────────────────────────


def _download_source(root: Path) -> None:
    """从 GitHub archive 下载源码 zip 并解压到当前目录。"""
    if _has_source(root):
        print("      源码已就位,跳过下载。")
        return

    print(f"      仓库: {GITHUB_REPO}")
    zip_path = None
    # 依次尝试直连和镜像
    for i, url in enumerate(GH_MIRRORS):
        label = "直连" if i == 0 else f"镜像{i}"
        try:
            print(f"      下载源码（{label}）…", end=" ", flush=True)
            tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
            tmp.close()
            zip_path = Path(tmp.name)
            urllib.request.urlretrieve(url, str(zip_path))
            size = zip_path.stat().st_size
            print(f"      [OK] {_human_size(size)}")
            break
        except Exception as exc:
            print(f"失败 ({exc})")
            if zip_path and zip_path.exists():
                zip_path.unlink()
            zip_path = None

    if zip_path is None:
        _fail(
            "无法下载源码。\n"
            "请检查网络连接,或手动从 GitHub 下载:\n"
            f"  https://github.com/{GITHUB_REPO}"
        )

    # 解压
    print("      解压源码…", end=" ", flush=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            # GitHub archive 内目录形如 StarGazerQQD-BiliLiveCut-<sha>/
            repo_prefix = None
            for name in zf.namelist():
                parts = name.split("/")
                if len(parts) >= 2 and parts[0].startswith("StarGazerQQD-BiliLiveCut-"):
                    repo_prefix = parts[0] + "/"
                    break

            if repo_prefix is None:
                raise RuntimeError("无法识别 GitHub archive 内部结构")

            # V0.1.8.2:从仓库根目录提取源码(不再依赖 Publish-PnP 子目录)。
            # 提取 app/ config/ pyproject.toml 到工作目录。
            # requirements-bundle.txt 从 Publish-PnP/ 提取到根目录。
            for member in zf.namelist():
                if not member.startswith(repo_prefix) or member == repo_prefix:
                    continue
                rel = member[len(repo_prefix):]
                # requirements-bundle.txt 特殊处理:从 Publish-PnP/ 提取到根
                if rel == "Publish-PnP/requirements-bundle.txt":
                    target = root / "requirements-bundle.txt"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(member))
                    continue
                # 跳过 Publish-PnP 自身的其他文件
                if "/" in rel and rel.split("/", 1)[0] in ("Publish-PnP",):
                    continue
                # 只提取需要的目录和文件
                keep = (
                    rel.startswith("app/") or rel.startswith("config/") or
                    rel == "pyproject.toml" or
                    rel == "setup.py" or rel == "setup_c.py" or
                    rel == "build_rust.py"
                )
                if not keep:
                    continue
                target = root / rel
                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(member))
        print("[OK]")
    except Exception as exc:
        _fail(f"解压源码失败: {exc}")
    finally:
        zip_path.unlink()

    # 验证
    if not _has_source(root):
        _fail("源码解压后验证失败:缺少 app/cli.py 或 requirements-bundle.txt")


# ── 步骤 4: 下载 Whisper 模型 ───────────────────────────────────────


def _download_model(venv_python: Path, root: Path) -> None:
    """下载 large-v3-turbo 模型（约 1.6GB,需要 venv 中的 huggingface_hub）。"""
    model_dir = root / "models" / MODEL_DIRNAME
    if (model_dir / "model.bin").exists():
        size = _human_size((model_dir / "model.bin").stat().st_size)
        print(f"      模型已就位 ({size}),跳过下载。")
        return

    model_dir.parent.mkdir(parents=True, exist_ok=True)

    print("      模型: large-v3-turbo（约 1.6GB,请耐心等待）…")
    print("      镜像: hf-mirror.com（HuggingFace 国内镜像）")

    # 在 venv 中运行模型下载（需要 huggingface_hub）
    script = f'''
import os, sys
os.environ["HF_ENDPOINT"] = "{HF_MIRROR}"
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
target = r"{model_dir}"
if os.path.exists(os.path.join(target, "model.bin")):
    sys.exit(0)
try:
    from huggingface_hub import snapshot_download
    snapshot_download(repo_id="{MODEL_REPO}", local_dir=target, token=False)
except Exception as e:
    print(f"[提示] snapshot 失败({{type(e).__name__}}),改为逐文件下载。")
    from huggingface_hub import hf_hub_download
    for fn in ["config.json", "model.bin", "tokenizer.json"]:
        print(f"下载 {{fn}} …")
        hf_hub_download(repo_id="{MODEL_REPO}", filename=fn, local_dir=target, token=False)
    for fn in ["vocabulary.txt", "vocabulary.json", "preprocessor_config.json"]:
        try:
            hf_hub_download(repo_id="{MODEL_REPO}", filename=fn, local_dir=target, token=False)
        except Exception:
            pass
'''

    try:
        result = subprocess.run(
            [str(venv_python), "-c", script],
            check=True,
            timeout=3600,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            print(result.stdout.strip())
    except subprocess.CalledProcessError as exc:
        print(exc.stderr.strip() if exc.stderr else str(exc))
        _fail(
            "模型下载失败。请检查网络,或手动下载后放入 models/ 目录。\n"
            f"  模型仓库: {MODEL_REPO}"
        )
    except subprocess.TimeoutExpired:
        _fail("模型下载超时（>1小时）。请检查网络后重试。")

    if not (model_dir / "model.bin").exists():
        _fail("模型下载后验证失败:未找到 model.bin")

    size = _human_size((model_dir / "model.bin").stat().st_size)
    print(f"      模型就位: {size}")


# ── 步骤 5: 下载 FFmpeg ────────────────────────────────────────────


def _download_ffmpeg(root: Path) -> None:
    """下载 ffmpeg/ffprobe 到 bin/（约 80MB）。"""
    bin_dir = root / "bin"

    # 检查是否已存在
    for name in ("ffmpeg.exe", "ffmpeg"):
        if (bin_dir / name).exists():
            print("      FFmpeg 已就位,跳过下载。")
            return

    bin_dir.mkdir(parents=True, exist_ok=True)

    if sys.platform != "win32":
        # 非 Windows:尝试从系统 PATH 复制
        suffix = ""
        got = 0
        for name in ("ffmpeg", "ffprobe"):
            src = shutil.which(name)
            if src:
                shutil.copy2(src, bin_dir / f"{name}{suffix}")
                got += 1
        if got == 2:
            print("      FFmpeg: 已从系统复制。")
            return
        _fail("请先安装 FFmpeg。可运行: apt install ffmpeg 或 brew install ffmpeg")

    # Windows:下载 BtbN 静态构建
    wanted = ["ffmpeg.exe", "ffprobe.exe"]
    urls = [
        (FFMPEG_WIN_URL, "直连"),
        (f"https://gh-proxy.com/{FFMPEG_WIN_URL}", "镜像"),
    ]

    zip_file = None
    for url, label in urls:
        try:
            print(f"      下载 FFmpeg（{label},约 80MB）…", end=" ", flush=True)
            tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
            tmp.close()
            zip_file = Path(tmp.name)
            urllib.request.urlretrieve(url, str(zip_file))
            print(f"[OK] {_human_size(zip_file.stat().st_size)}")
            break
        except Exception as exc:
            print(f"失败 ({exc})")
            if zip_file and zip_file.exists():
                zip_file.unlink()
            zip_file = None

    if zip_file is None:
        # 回退:从系统 PATH 复制
        print("      下载失败,尝试从系统 PATH 复制…")
        suffix = ".exe"
        got = 0
        for name in ("ffmpeg", "ffprobe"):
            src = shutil.which(name)
            if src:
                shutil.copy2(src, bin_dir / f"{name}{suffix}")
                print(f"      已复制 {name}: {src}")
                got += 1
        if got == 2:
            return
        _fail(
            "无法获取 FFmpeg。请手动下载后放入 bin/ 目录,\n"
            "或从 https://ffmpeg.org/download.html 安装并加入 PATH。"
        )

    # 解压
    print("      解压 FFmpeg…", end=" ", flush=True)
    try:
        with zipfile.ZipFile(zip_file) as zf:
            for member in zf.namelist():
                base = member.rsplit("/", 1)[-1]
                if base in wanted and member.endswith("bin/" + base):
                    (bin_dir / base).write_bytes(zf.read(member))
        print("[OK]")
    except Exception as exc:
        _fail(f"解压 FFmpeg 失败: {exc}")
    finally:
        zip_file.unlink()

    if not (bin_dir / "ffmpeg.exe").exists():
        _fail("FFmpeg 解压后验证失败:未找到 ffmpeg.exe")


# ── 步骤 6: 生成 .env ──────────────────────────────────────────────


def _ensure_env(root: Path) -> None:
    """如果 .env 不存在则生成。"""
    env_path = root / ".env"
    if env_path.exists():
        return
    env_path.write_text(ENV_TEMPLATE, encoding="utf-8")
    print("      .env 配置文件已生成。")


# ── 主流程 ────────────────────────────────────────────────────────


def main() -> None:
    """全自展开启动器:一个 .exe 从零搭建完整运行环境。"""
    root = Path(sys.argv[0]).resolve().parent
    os.chdir(str(root))
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    print("=" * 60)
    print(f"  {APP_NAME} {VERSION} — 即插即用启动器（全自动自展开）")
    print("=" * 60)
    print(f"  工作目录: {root}")
    print()

    # 扫描并报告当前状态
    _scan_and_report(root)

    # ================================================================
    # 1) 检测 Python
    # ================================================================
    _step_header("1/7 检测 Python", root)
    venv_python = root / VENV_DIR / "Scripts" / "python.exe"
    if not venv_python.exists():
        system_py = _find_system_python()
        if system_py is None:
            _fail(
                "未找到 Python 3.11+。\n"
                "请从 https://www.python.org/downloads/ 下载并安装 Python 3.11 或更高版本,\n"
                "安装时勾选「Add Python to PATH」。"
            )
        try:
            ver = subprocess.check_output(
                [str(system_py), "--version"], text=True, timeout=10
            ).strip()
        except Exception:
            ver = "(未知)"
        print(f"      {ver}  @ {system_py}")
        venv_check = system_py
    else:
        system_py = venv_python
        print("      虚拟环境已存在。")
    venv_check = system_py

    # 检测 Python 版本
    try:
        result = subprocess.run(
            [str(venv_check), "-c", "import sys; print(sys.version_info[:2])"],
            capture_output=True, text=True, timeout=10,
        )
        v_tuple = tuple(int(x) for x in result.stdout.strip().strip("()").split(","))
        if v_tuple >= (3, 13):
            print("      [注意] Python 3.13+ 可能与部分依赖不兼容。")
            print("             如遇到安装失败,建议换用 Python 3.11 或 3.12。")
    except Exception:
        pass

    print()

    # ================================================================
    # 2) 下载源码
    # ================================================================
    _step_header("2/7 源码", root)
    _download_source(root)
    print()

    # ================================================================
    # 3) 创建虚拟环境
    # ================================================================
    _step_header("3/7 虚拟环境", root)
    if not venv_python.exists():
        print("      创建 .venv …", end=" ", flush=True)
        try:
            subprocess.run(
                [str(system_py), "-m", "venv", VENV_DIR],
                check=True,
                timeout=120,
                capture_output=True,
            )
            print("[OK]")
        except subprocess.CalledProcessError as exc:
            _fail(f"创建虚拟环境失败: {exc.stderr.decode() if exc.stderr else exc}")
    else:
        print("      已存在,跳过。")
    print()

    # ================================================================
    # 4) 安装依赖
    # ================================================================
    _step_header("4/7 安装依赖", root)
    # 检查依赖是否已安装
    deps_ok = False
    try:
        subprocess.run(
            [str(venv_python), "-c",
             "import faster_whisper, fastapi, uvicorn, sqlmodel; print('ok')"],
            check=True, capture_output=True, timeout=30,
        )
        deps_ok = True
        print("      依赖已安装,校验通过。")
    except subprocess.CalledProcessError:
        pass

    if not deps_ok:
        wheels = root / WHEELS_DIR
        if wheels.exists() and list(wheels.glob("*.whl")):
            # 离线安装路径（已有预打包 wheel）
            print("      检测到 vendor/wheels,使用离线安装（无需联网）…")
            try:
                subprocess.run(
                    [
                        str(venv_python), "-m", "pip", "install",
                        "--no-index", "--find-links", str(wheels),
                        "-r", REQUIREMENTS,
                    ],
                    check=True, timeout=600,
                )
                print("      [OK] 离线安装完成")
            except subprocess.CalledProcessError:
                _fail("离线安装失败。请检查 vendor/wheels/ 中 wheel 是否齐全且平台一致。")
        else:
            # 联网安装路径
            print("      联网安装（清华+阿里云镜像）…")
            print("      （首次安装约 2-5 分钟,请耐心等待）")
            try:
                _pip_install(venv_python, root)
                print("      [OK] 依赖安装完成")
            except subprocess.CalledProcessError as exc:
                msg = "依赖安装失败。常见原因:\n"
                msg += "  1) Python 3.13 不兼容:请换用 Python 3.11 或 3.12\n"
                msg += "  2) 网络问题:请检查是否能访问 mirrors.aliyun.com 和 pypi.tuna.tsinghua.edu.cn\n"
                _fail(msg)
            except subprocess.TimeoutExpired:
                _fail("依赖安装超时（>15分钟）。请检查网络后重试。")
    print()

    # ================================================================
    # 5) 下载模型
    # ================================================================
    _step_header("5/7 Whisper 模型", root)
    _download_model(venv_python, root)
    print()

    # ================================================================
    # 6) 下载 FFmpeg
    # ================================================================
    _step_header("6/7 FFmpeg", root)
    _download_ffmpeg(root)
    print()

    # ================================================================
    # 7) 生成 .env
    # ================================================================
    _step_header("7/7 配置文件", root)
    _ensure_env(root)
    print()

    # ================================================================
    # 启动服务
    # ================================================================
    print("=" * 60)
    print("  环境就绪,启动 Web 控制台…")
    print()
    print(f"  浏览器将自动打开: http://127.0.0.1:8000")
    print("  按 Ctrl+C 停止服务。")
    print("=" * 60)
    print()

    env = os.environ.copy()
    env["WHISPER_MODEL"] = str(root / "models" / MODEL_DIRNAME)
    bin_dir = root / "bin"
    if (bin_dir / "ffmpeg.exe").exists():
        env["FFMPEG_PATH"] = str(bin_dir / "ffmpeg.exe")
        env["FFPROBE_PATH"] = str(bin_dir / "ffprobe.exe")
        env["PATH"] = str(bin_dir) + ";" + env.get("PATH", "")

    webbrowser.open("http://127.0.0.1:8000")

    try:
        subprocess.run(
            [
                str(venv_python), "-m", "app.cli", "serve",
                "--host", "127.0.0.1", "--port", "8000",
            ],
            env=env,
            cwd=str(root),
        )
    except KeyboardInterrupt:
        print("\n服务已停止。")
    except Exception:
        print("\n服务异常退出:")
        traceback.print_exc()
        print("\n按 Enter 键退出…")
        input()


if __name__ == "__main__":
    main()
