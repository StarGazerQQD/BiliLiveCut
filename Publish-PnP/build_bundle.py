"""即插即用版打包脚本。

在一台**能联网**的机器上运行一次,把「模型 + 依赖 + 源码」全部封装进 ``Publish-PnP/``:

1. **模型**:下载 faster-whisper 的 ``large-v3-turbo``(Systran 转换版)到
   ``Publish-PnP/models/whisper-large-v3-turbo``(境内默认走 hf-mirror.com 镜像);
2. **依赖**:按 ``requirements-bundle.txt`` 把全部 wheel 下载到 ``Publish-PnP/vendor/wheels``
   (供离线 ``install`` 使用,封装所有外部库);
3. **源码**:把主工程的 ``app/`` ``config/`` 等复制进 ``Publish-PnP/``,使其自包含。

打包完成后,把整个 ``Publish-PnP/`` 目录拷到目标机器,依次运行 ``install`` 与 ``run`` 即可
离线启动;其所用 Whisper 固定为包内的 ``large-v3-turbo``。

用法::

    python build_bundle.py                 # 全量打包(模型+依赖+源码)
    python build_bundle.py --skip-model    # 跳过模型下载
    python build_bundle.py --skip-wheels   # 跳过依赖封装
    python build_bundle.py --only-source   # 仅复制源码
    python build_bundle.py --hf-mirror https://hf-mirror.com --pip-index <url>
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

# faster-whisper 的 large-v3-turbo(CTranslate2 转换版)。
# 该别名在 faster-whisper 的 _MODELS 中映射到此仓库。
MODEL_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
MODEL_DIRNAME = "whisper-large-v3-turbo"

# FFmpeg 静态构建(Windows x64;BtbN 提供 ffmpeg.exe/ffprobe.exe 于 bin/ 下)。
FFMPEG_WIN_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)
# GitHub 在境内常不稳,提供代理镜像前缀作为回退。
GH_PROXY_PREFIX = "https://gh-proxy.com/"
# 下载超时(秒),避免网络不通时长时间卡死。
DOWNLOAD_TIMEOUT_S = 30

# pip 镜像(中国内地更快):阿里云优先、清华备用。环境变量 PIP_INDEX_URL/PIP_EXTRA_INDEX_URL 可覆盖。
PIP_INDEX_PRIMARY = "https://mirrors.aliyun.com/pypi/simple/"
PIP_INDEX_EXTRAS = [
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://pypi.tuna.tsinghua.edu.cn/simple",
]
# 上述镜像域名(用于 --trusted-host,规避个别环境的证书问题)。
PIP_TRUSTED_HOSTS = ["mirrors.aliyun.com", "pypi.tuna.tsinghua.edu.cn"]

DIST_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DIST_DIR.parent
# 需要复制进即插即用包的源码/资源(相对主工程根)。
# 注意:主工程 README.md 会被复制为 README_MAIN.md,避免覆盖 Publish-PnP 自己的说明。
SOURCE_ITEMS = ["app", "config", "pyproject.toml"]

MANIFEST_PATH = DIST_DIR / "manifest.json"

# 模型目录内的期望文件:required 缺失即判失败;recommended 缺失仅告警。
MODEL_REQUIRED = ["model.bin", "config.json", "tokenizer.json"]
MODEL_RECOMMENDED = ["vocabulary.txt", "vocabulary.json", "preprocessor_config.json"]

# 离线安装成功所需的关键(含原生)传递依赖,缺失会告警提示补齐。
CRITICAL_TRANSITIVE = [
    "ctranslate2", "tokenizers", "onnxruntime", "av",   # faster-whisper 运行时
    "starlette", "anyio", "click", "h11", "sniffio",     # web / http 栈
    "certifi", "idna", "httpcore",
    "annotated_types", "typing_extensions",              # pydantic 依赖
]


def log(msg: str) -> None:
    """打印带前缀的进度信息。

    :param msg: 文本。
    """
    print(f"[build_bundle] {msg}", flush=True)


def copy_source() -> None:
    """把主工程源码复制进 Publish-PnP(使其自包含)。"""
    for name in SOURCE_ITEMS:
        src = PROJECT_ROOT / name
        dst = DIST_DIR / name
        if not src.exists():
            log(f"跳过不存在的源:{src}")
            continue
        if src.is_dir():
            shutil.copytree(
                src,
                dst,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        else:
            shutil.copy2(src, dst)
        log(f"已复制源码:{name}")
    # 主工程 README 另存为 README_MAIN.md,避免覆盖 Publish-PnP 说明。
    main_readme = PROJECT_ROOT / "README.md"
    if main_readme.exists():
        shutil.copy2(main_readme, DIST_DIR / "README_MAIN.md")
        log("已复制源码:README.md -> README_MAIN.md")


def download_model(hf_mirror: str) -> None:
    """下载 large-v3-turbo 模型到 Publish-PnP/models。

    :param hf_mirror: HuggingFace 镜像地址(境内建议 https://hf-mirror.com)。
    """
    target = DIST_DIR / "models" / MODEL_DIRNAME
    if (target / "model.bin").exists():
        log(f"模型已存在,跳过下载:{target}")
        return
    if hf_mirror:
        os.environ["HF_ENDPOINT"] = hf_mirror
        log(f"使用 HuggingFace 镜像:{hf_mirror}")
    # 本仓库为公开模型:强制匿名访问,避免本机残留的无效 token 触发 401。
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    try:
        from huggingface_hub import hf_hub_download, snapshot_download
    except ImportError:
        log("未安装 huggingface_hub,请先 `pip install huggingface_hub` 再打包模型。")
        raise

    log(f"开始下载模型 {MODEL_REPO}(约 1.6GB,请耐心等待)…")
    # 优先 snapshot;若镜像的 /api/models 元数据端点返回 401,则回退为逐文件下载
    # (直接走 /resolve/ 端点,镜像对其支持更稳)。
    try:
        snapshot_download(repo_id=MODEL_REPO, local_dir=str(target), token=False)
    except Exception as exc:  # noqa: BLE001 — 回退逐文件下载
        log(f"[提示] snapshot 失败({type(exc).__name__}),改为逐文件下载。")
        required = ["config.json", "model.bin", "tokenizer.json"]
        optional = ["vocabulary.txt", "vocabulary.json", "preprocessor_config.json"]
        for fn in required:
            log(f"下载 {fn} …")
            hf_hub_download(repo_id=MODEL_REPO, filename=fn, local_dir=str(target), token=False)
        for fn in optional:
            try:
                hf_hub_download(repo_id=MODEL_REPO, filename=fn, local_dir=str(target), token=False)
            except Exception:  # noqa: BLE001 — 可选文件不存在则跳过
                pass
    log(f"模型已就位:{target}")


def _pip_index_args(pip_index: str) -> list[str]:
    """构造 pip 的镜像参数:主 index + 两条备选 extra-index(国内更快)。

    :param pip_index: 用户指定的主镜像;留空则用清华源。
    :returns: 可拼接进 pip 命令的参数列表。
    """
    primary = pip_index or PIP_INDEX_PRIMARY
    args = ["-i", primary]
    for extra in PIP_INDEX_EXTRAS:
        if extra != primary:
            args += ["--extra-index-url", extra]
    for host in PIP_TRUSTED_HOSTS:
        args += ["--trusted-host", host]
    return args


def vendor_wheels(pip_index: str) -> None:
    """把 requirements-bundle.txt 的全部依赖下载为 wheel 到 vendor/wheels。

    使用清华为主镜像、阿里云为备选(extra-index),两条链路在中国内地都很快。

    :param pip_index: 可选的 pip 主镜像(留空用清华);备选镜像始终附加。
    """
    wheels = DIST_DIR / "vendor" / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    req = DIST_DIR / "requirements-bundle.txt"
    index_args = _pip_index_args(pip_index)
    log("开始封装依赖 wheel …")
    subprocess.run(
        [sys.executable, "-m", "pip", "download", "-r", str(req), "-d", str(wheels)] + index_args,
        check=True,
    )
    # 一并封装 pip/setuptools/wheel,保证离线机也能装。
    subprocess.run(
        [sys.executable, "-m", "pip", "download", "pip", "setuptools", "wheel",
         "-d", str(wheels)] + index_args,
        check=True,
    )
    count = len(list(wheels.glob("*.whl"))) + len(list(wheels.glob("*.tar.gz")))
    log(f"依赖封装完成,共 {count} 个包 -> {wheels}")


def _find_bin(bin_dir: Path, names: list[str]) -> Path | None:
    """在 bin 目录中查找可执行文件(兼容有无 .exe 后缀)。

    :param bin_dir: bin 目录。
    :param names: 候选文件名列表。
    :returns: 命中的路径;都不存在返回 ``None``。
    """
    for n in names:
        p = bin_dir / n
        if p.exists():
            return p
    return None


def _download_to(url: str, dest: Path, timeout: int = DOWNLOAD_TIMEOUT_S) -> None:
    """带超时地把 URL 下载到本地文件(流式写,避免占用大量内存)。

    :param url: 下载地址。
    :param dest: 目标文件。
    :param timeout: 连接/读取超时(秒)。
    """
    with urllib.request.urlopen(url, timeout=timeout) as resp, open(dest, "wb") as f:  # noqa: S310
        shutil.copyfileobj(resp, f)


def _copy_system_ffmpeg(bin_dir: Path) -> bool:
    """从系统 PATH 复制 ffmpeg/ffprobe 到 bin(下载失败时的回退)。

    :param bin_dir: 目标 bin 目录。
    :returns: 两者都复制成功返回 ``True``。
    """
    suffix = ".exe" if platform.system() == "Windows" else ""
    got = 0
    for name in ("ffmpeg", "ffprobe"):
        src = shutil.which(name)
        if src:
            shutil.copy2(src, bin_dir / f"{name}{suffix}")
            log(f"已从系统复制 {name}: {src}")
            got += 1
        else:
            log(f"未在系统 PATH 找到 {name}。")
    return got == 2


def _extract_ffmpeg_from_zip(zip_path: Path, bin_dir: Path, wanted: list[str]) -> None:
    """从 ffmpeg zip 中提取指定可执行文件到 bin 目录。

    :param zip_path: zip 文件路径。
    :param bin_dir: 目标 bin 目录。
    :param wanted: 需要提取的文件名(如 ``ffmpeg.exe``)。
    """
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            # 安全防护: 禁止绝对路径和 .. 的路径名, 防止 ZipSlip 路径遍历
            if member.startswith("/") or ".." in member.split("/"):
                print(f"跳过危险路径: {member}")
                continue
            base = member.rsplit("/", 1)[-1]
            if base in wanted and member.endswith("bin/" + base):
                (bin_dir / base).write_bytes(zf.read(member))
                log(f"已提取 {base}")


def download_ffmpeg(ffmpeg_zip: str, ffmpeg_url: str) -> None:
    """把 ffmpeg/ffprobe 整合进包内 ``bin/``(免目标机另行下载)。

    Windows:下载 BtbN 静态构建 zip 并解压出 ``ffmpeg.exe`` / ``ffprobe.exe``;
    也可用 ``--ffmpeg-zip`` 指定本地已下载的 zip(便于离线)。
    其它系统:尝试从系统 PATH 复制现有 ffmpeg/ffprobe。

    :param ffmpeg_zip: 本地 ffmpeg zip 路径(可空)。
    :param ffmpeg_url: 下载地址(可空,Windows 默认 BtbN)。
    """
    bin_dir = DIST_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if _find_bin(bin_dir, ["ffmpeg.exe", "ffmpeg"]):
        log(f"ffmpeg 已存在,跳过:{bin_dir}")
        return

    system = platform.system()
    if system == "Windows":
        wanted = ["ffmpeg.exe", "ffprobe.exe"]
        if ffmpeg_zip:
            try:
                _extract_ffmpeg_from_zip(Path(ffmpeg_zip), bin_dir, wanted)
                log(f"已从本地压缩包整合:{ffmpeg_zip}")
            except Exception as exc:  # noqa: BLE001
                log(f"[警告] 解压本地 ffmpeg 压缩包失败:{exc}")
        else:
            base = ffmpeg_url or FFMPEG_WIN_URL
            # 依次尝试:直连 -> GitHub 代理镜像;都失败再回退系统复制。
            for url in (base, GH_PROXY_PREFIX + base):
                try:
                    log(f"下载 ffmpeg(约 80-100MB,{DOWNLOAD_TIMEOUT_S}s 超时):{url}")
                    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
                    tmp.close()
                    _download_to(url, Path(tmp.name))
                    _extract_ffmpeg_from_zip(Path(tmp.name), bin_dir, wanted)
                    break
                except Exception as exc:  # noqa: BLE001 — 换下一个来源
                    log(f"[警告] 下载失败({exc}),尝试下一来源。")
            if not _find_bin(bin_dir, ["ffmpeg.exe"]):
                log("下载均失败,尝试从系统 PATH 复制 ffmpeg。")
                _copy_system_ffmpeg(bin_dir)
    else:
        _copy_system_ffmpeg(bin_dir)

    if _find_bin(bin_dir, ["ffmpeg.exe", "ffmpeg"]):
        log(f"ffmpeg 已就位:{bin_dir}")
    else:
        log("ffmpeg 整合失败:请检查网络或用 --ffmpeg-zip 指定本地压缩包。")


# --------------------------------------------------------------------------- #
# 自校验清单 / 一键体检
# --------------------------------------------------------------------------- #
def _normalize(name: str) -> str:
    """把包名归一化(小写,``-``/``_``/``.`` 视为等价)。

    :param name: 原始包名。
    :returns: 归一化名称。
    """
    return re.sub(r"[-_.]+", "_", name.strip().lower())


def _requirement_names() -> list[str]:
    """从 requirements-bundle.txt 解析顶层依赖名(去掉版本/extras/注释)。

    :returns: 归一化后的依赖名列表。
    """
    req = DIST_DIR / "requirements-bundle.txt"
    names: list[str] = []
    if not req.exists():
        return names
    for line in req.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        # 去掉版本约束与 extras:uvicorn[standard]>=0.30 -> uvicorn
        base = re.split(r"[<>=!~\[ ]", line, maxsplit=1)[0]
        if base:
            names.append(_normalize(base))
    return names


def _wheel_package_names(wheels_dir: Path) -> set[str]:
    """扫描 vendor/wheels,提取已封装的包名集合(归一化)。

    :param wheels_dir: wheel 目录。
    :returns: 归一化包名集合。
    """
    names: set[str] = set()
    if not wheels_dir.exists():
        return names
    for whl in wheels_dir.glob("*.whl"):
        # wheel 文件名:{distribution}-{version}-...whl,distribution 用下划线且不含连字符。
        names.add(_normalize(whl.name.split("-", 1)[0]))
    for sdist in list(wheels_dir.glob("*.tar.gz")) + list(wheels_dir.glob("*.zip")):
        stem = sdist.name.rsplit(".tar.gz", 1)[0].rsplit(".zip", 1)[0]
        names.add(_normalize(stem.rsplit("-", 1)[0]))
    return names


def _current_platform() -> dict:
    """返回当前机器的平台信息(用于 wheel 兼容性校验)。

    :returns: ``{python, python_tag, system, machine}``。
    """
    return {
        "python": platform.python_version(),
        "python_tag": f"cp{sys.version_info.major}{sys.version_info.minor}",
        "system": platform.system(),
        "machine": platform.machine(),
    }


def _read_prev_build_platform() -> dict | None:
    """读取既有 manifest.json 记录的打包平台(供体检对比)。

    :returns: 打包平台字典;无记录返回 ``None``。
    """
    if not MANIFEST_PATH.exists():
        return None
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("build_platform") or data.get("platform")


def _minor(version: str) -> str:
    """取 Python 版本的主.次部分(如 ``3.12.4`` -> ``3.12``)。

    :param version: 版本串。
    :returns: 主.次。
    """
    return ".".join(version.split(".")[:2])


def _platform_check(build_pf: dict | None, current: dict) -> dict:
    """对比打包平台与当前机器,给出兼容性告警。

    wheel 与「操作系统 + CPU 架构 + CPython 次版本(ABI)」强相关,任一不一致都会导致
    离线安装失败(例如把 Linux 的 wheel 拿到 Windows 装)。

    :param build_pf: 打包平台(可空)。
    :param current: 当前平台。
    :returns: ``{ok, recorded, build, current, warnings}``。
    """
    if not build_pf:
        return {
            "ok": True, "recorded": False, "build": None, "current": current,
            "warnings": ["无打包平台记录(manifest 缺失或为旧版),跳过平台一致性校验。"],
        }
    warnings: list[str] = []
    ok = True
    if build_pf.get("system") != current["system"]:
        ok = False
        warnings.append(
            f"操作系统不一致:打包={build_pf.get('system')} 当前={current['system']}"
            "(wheel 不通用,离线安装会失败)"
        )
    if build_pf.get("machine") != current["machine"]:
        ok = False
        warnings.append(
            f"CPU 架构不一致:打包={build_pf.get('machine')} 当前={current['machine']}"
        )
    if _minor(build_pf.get("python", "")) != _minor(current["python"]):
        ok = False
        warnings.append(
            f"Python 次版本不一致:打包={build_pf.get('python')} 当前={current['python']}"
            "(cpXY ABI 不匹配,多数 wheel 无法安装)"
        )
    return {"ok": ok, "recorded": True, "build": build_pf, "current": current, "warnings": warnings}


def collect_status(build_platform: dict | None = None) -> dict:
    """采集当前 Publish-PnP 包的完整状态(供写清单与体检共用)。

    :param build_platform: 打包平台;打包时传当前平台,体检时传 manifest 记录的平台。
    :returns: 状态字典(含各部分 ``ok`` 与总 ``ok``)。
    """
    current = _current_platform()
    platform_check = _platform_check(build_platform, current)

    model_dir = DIST_DIR / "models" / MODEL_DIRNAME
    model_bin = model_dir / "model.bin"
    req_missing = [f for f in MODEL_REQUIRED if not (model_dir / f).exists()]
    rec_present = [f for f in MODEL_RECOMMENDED if (model_dir / f).exists()]
    model = {
        "dir": str(model_dir),
        "model_bin": model_bin.exists(),
        "model_bin_size": model_bin.stat().st_size if model_bin.exists() else 0,
        "required_missing": req_missing,
        "recommended_present": rec_present,
        "ok": model_bin.exists() and model_bin.stat().st_size > 0 and not req_missing,
    }

    wheels_dir = DIST_DIR / "vendor" / "wheels"
    pkgs = _wheel_package_names(wheels_dir)
    reqs = _requirement_names()
    req_pkg_missing = sorted(n for n in reqs if n not in pkgs)
    crit_missing = sorted(_normalize(n) for n in CRITICAL_TRANSITIVE if _normalize(n) not in pkgs)
    count = len(list(wheels_dir.glob("*.whl"))) + len(list(wheels_dir.glob("*.tar.gz")))
    wheels = {
        "dir": str(wheels_dir),
        "count": count,
        "required_missing": req_pkg_missing,
        "critical_transitive_missing": crit_missing,
        "ok": count > 0 and not req_pkg_missing,
    }

    bin_dir = DIST_DIR / "bin"
    ffmpeg_bin = _find_bin(bin_dir, ["ffmpeg.exe", "ffmpeg"])
    ffprobe_bin = _find_bin(bin_dir, ["ffprobe.exe", "ffprobe"])
    ffmpeg = {
        "dir": str(bin_dir),
        "ffmpeg": ffmpeg_bin is not None,
        "ffprobe": ffprobe_bin is not None,
        "ok": ffmpeg_bin is not None and ffprobe_bin is not None,
    }

    src_items = {name: (DIST_DIR / name).exists() for name in SOURCE_ITEMS}
    source = {"items": src_items, "ok": all(src_items.values())}
    env_present = (DIST_DIR / ".env").exists()

    overall = (
        model["ok"] and wheels["ok"] and ffmpeg["ok"]
        and source["ok"] and env_present and platform_check["ok"]
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "build_platform": build_platform,
        "runtime_platform": current,
        "platform_check": platform_check,
        "whisper_model": MODEL_REPO,
        "model": model,
        "ffmpeg": ffmpeg,
        "wheels": wheels,
        "source": source,
        "env_present": env_present,
        "ok": overall,
    }


def write_manifest(status: dict) -> None:
    """把状态写入 manifest.json。

    :param status: :func:`collect_status` 的结果。
    """
    MANIFEST_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"已生成自校验清单:{MANIFEST_PATH}")


def print_report(status: dict) -> bool:
    """打印体检报告,返回是否全部通过。

    :param status: 状态字典。
    :returns: 全部通过返回 ``True``。
    """
    def mark(ok: bool) -> str:
        return "[OK]  " if ok else "[FAIL]"

    print("\n===== 即插即用版体检报告 =====")

    pc = status["platform_check"]
    if not pc["recorded"]:
        print("[WARN] 平台一致性:无打包记录,跳过校验")
    else:
        b, c = pc["build"], pc["current"]
        bs = f"{b.get('system')}/{b.get('machine')}/py{b.get('python')}"
        cs = f"{c['system']}/{c['machine']}/py{c['python']}"
        print(f"{mark(pc['ok'])} 平台一致性:打包={bs} 当前={cs}")
    for warn in pc["warnings"]:
        print(f"        {warn}")

    m = status["model"]
    size_mb = m["model_bin_size"] / 1024 / 1024
    print(f"{mark(m['ok'])} 模型 large-v3-turbo:model.bin={'有' if m['model_bin'] else '无'} "
          f"({size_mb:.0f} MB)")
    if m["required_missing"]:
        print(f"        缺少必需文件:{', '.join(m['required_missing'])}")

    f = status["ffmpeg"]
    print(f"{mark(f['ok'])} FFmpeg:ffmpeg={'有' if f['ffmpeg'] else '无'} "
          f"ffprobe={'有' if f['ffprobe'] else '无'}")
    if not f["ok"]:
        print("        缺少 ffmpeg/ffprobe:请打包时不要跳过,或用 --ffmpeg-zip 指定本地压缩包。")

    w = status["wheels"]
    print(f"{mark(w['ok'])} 依赖 wheel:共 {w['count']} 个")
    if w["required_missing"]:
        print(f"        缺少顶层依赖:{', '.join(w['required_missing'])}")
    if w["critical_transitive_missing"]:
        print(f"        [警告] 可能缺少关键传递依赖:{', '.join(w['critical_transitive_missing'])}")

    s = status["source"]
    missing_src = [k for k, v in s["items"].items() if not v]
    print(f"{mark(s['ok'])} 源码/配置:{'齐全' if s['ok'] else '缺失 ' + ', '.join(missing_src)}")
    print(f"{mark(status['env_present'])} 配置文件 .env")

    print("-----------------------------")
    print(f"总体:{'[PASS] 可分发' if status['ok'] else '[FAIL] 不完整,请按上面提示补齐后重跑'}")
    print("=============================\n")
    return bool(status["ok"])


def _purge_dir(path: Path, patterns: list[str]) -> int:
    """删除目录中匹配的文件(用于清理错平台的旧产物)。

    :param path: 目录。
    :param patterns: glob 模式列表。
    :returns: 删除的文件数。
    """
    n = 0
    if not path.exists():
        return 0
    for pat in patterns:
        for f in path.glob(pat):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def repair(args: argparse.Namespace) -> bool:
    """自动修复:发现问题即自动下载/重建对应组件,而非仅提示。

    * 平台不一致 → 清掉错平台的 wheel 与 ffmpeg,按**当前机器**重新下载合适的组件;
    * 缺 wheel → 自动 ``pip download`` 补齐;
    * 缺 ffmpeg → 自动下载(失败则尝试从系统复制);
    * 缺模型 → 自动下载(默认走镜像);
    * 缺源码 → 尝试从主工程复制。

    每步独立容错,最后重新体检并把 ``build_platform`` 记为当前机器。

    :param args: 命令行参数(含 pip_index / hf_mirror / ffmpeg_zip / ffmpeg_url)。
    :returns: 修复后是否全部通过。
    """
    status = collect_status(_read_prev_build_platform())
    log("修复前体检:")
    print_report(status)

    pc = status["platform_check"]
    platform_mismatch = pc["recorded"] and not pc["ok"]

    def _try(step_name: str, fn) -> None:  # noqa: ANN001
        try:
            log(f"自动修复:{step_name} …")
            fn()
        except Exception as exc:  # noqa: BLE001 — 单步失败不阻断其它修复
            log(f"[警告] {step_name} 失败:{exc}")

    if platform_mismatch:
        log("检测到平台不一致 → 按当前机器重新下载合适的依赖与 ffmpeg。")
        purged = _purge_dir(DIST_DIR / "vendor" / "wheels", ["*.whl", "*.tar.gz", "*.zip"])
        purged += _purge_dir(DIST_DIR / "bin", ["ffmpeg*", "ffprobe*"])
        log(f"已清理错平台旧产物 {purged} 个。")
        _try("下载依赖 wheel(当前平台)", lambda: vendor_wheels(args.pip_index))
        _try("整合 ffmpeg(当前平台)", lambda: download_ffmpeg(args.ffmpeg_zip, args.ffmpeg_url))
    else:
        if not status["wheels"]["ok"]:
            _try("下载依赖 wheel", lambda: vendor_wheels(args.pip_index))
        if not status["ffmpeg"]["ok"]:
            _try("整合 ffmpeg", lambda: download_ffmpeg(args.ffmpeg_zip, args.ffmpeg_url))

    if not status["model"]["ok"]:
        _try("下载模型 large-v3-turbo", lambda: download_model(args.hf_mirror))
    if not status["source"]["ok"]:
        _try("复制源码", copy_source)

    # 修复后:以当前机器作为打包平台重新记录并体检。
    final = collect_status(_current_platform())
    write_manifest(final)
    log("修复后体检:")
    ok = print_report(final)
    return ok


def run_check(is_build: bool = False) -> bool:
    """执行一次体检(采集状态 -> 写清单 -> 打印报告)。

    :param is_build: 是否处于打包流程。打包时以当前机器作为"打包平台"记录;
        体检时读取既有 manifest 记录的打包平台用于对比,并保留该记录不被覆盖。
    :returns: 是否通过。
    """
    build_pf = _current_platform() if is_build else _read_prev_build_platform()
    status = collect_status(build_pf)
    write_manifest(status)
    return print_report(status)


def main() -> None:
    """解析参数并按需执行打包步骤。"""
    parser = argparse.ArgumentParser(description="BiliLiveCut 即插即用版打包器")
    parser.add_argument("--skip-model", action="store_true", help="跳过模型下载")
    parser.add_argument("--skip-wheels", action="store_true", help="跳过依赖封装")
    parser.add_argument("--skip-source", action="store_true", help="跳过源码复制")
    parser.add_argument("--skip-ffmpeg", action="store_true", help="跳过 ffmpeg 整合")
    parser.add_argument("--only-source", action="store_true", help="仅复制源码")
    parser.add_argument("--check", action="store_true", help="仅体检:核对模型/依赖/ffmpeg/平台")
    parser.add_argument(
        "--repair", action="store_true",
        help="自动修复:发现缺失/平台不一致即自动下载合适组件(全自动)",
    )
    parser.add_argument(
        "--hf-mirror", default="https://hf-mirror.com",
        help="HuggingFace 镜像地址(留空用官方)",
    )
    parser.add_argument("--pip-index", default="", help="pip 镜像索引地址")
    parser.add_argument("--ffmpeg-zip", default="", help="本地 ffmpeg 压缩包路径(离线整合用)")
    parser.add_argument("--ffmpeg-url", default="", help="ffmpeg 下载地址(默认 Windows BtbN)")
    args = parser.parse_args()

    if args.check:
        ok = run_check(is_build=False)
        sys.exit(0 if ok else 1)

    if args.repair:
        ok = repair(args)
        sys.exit(0 if ok else 1)

    if args.only_source:
        copy_source()
        log("完成(仅源码)。")
        return

    if not args.skip_source:
        copy_source()
    if not args.skip_wheels:
        vendor_wheels(args.pip_index)
    if not args.skip_ffmpeg:
        download_ffmpeg(args.ffmpeg_zip, args.ffmpeg_url)
    if not args.skip_model:
        download_model(args.hf_mirror)

    log("打包完成,开始自校验体检 …")
    ok = run_check(is_build=True)
    if ok:
        log("体检通过。下一步:目标机器运行 install(离线装依赖),再 run(启动)。")
    else:
        log("体检未通过:可运行 `python build_bundle.py --repair` 或 setup 一键自动修复。")
        sys.exit(1)


if __name__ == "__main__":
    main()
