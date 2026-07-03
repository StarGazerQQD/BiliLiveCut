# BiliLiveCut · 即插即用版(Public)

**版本:V0.1.3 Alpha** (`0.1.3-alpha`)

一个**自包含**的分发版:**一个 `launcher.exe` 即可从零搭建完整环境** — 源码、依赖、Whisper 模型、ffmpeg 全自动下载。
语音转写**固定使用 large-v3-turbo**。

> **V0.1.2 起**,``app/``、``config/``、``pyproject.toml`` 已随仓库分发:
> ``git clone`` 下来即可使用,无需先跑 ``build_bundle.py``。
> 模型(约 1.6GB)、wheel 依赖、ffmpeg 需要联网下载（见下方两种方式）。

## 两种使用方式

### 方式 A:双击 launcher.exe（推荐,有网 → 全自动）

直接**双击 `launcher.exe`**,**一个 exe 从零搭建完整环境**:

1. 自动检测系统 Python 3.11+
2. 从 GitHub 下载源码（app/config 等,~200KB）
3. 创建虚拟环境 + 联网安装依赖（清华+阿里云镜像,2-5 分钟）
4. 下载 Whisper large-v3-turbo 模型（hf-mirror.com,~1.6GB,最久）
5. 下载 ffmpeg/ffprobe（~80MB）
6. 生成 .env 配置 → 启动 Web 管理后台

> **零文件拷贝,断点续跑**:如果某一步中断,再次双击会自动从断点继续,已下载的组件不会重复下载。

### 方式 B:完整离线包（给无网机器）

在**能联网**的机器上运行一次打包,然后把整个 ``Public/`` 拷到离线目标机:

```powershell
pip install huggingface_hub
python build_bundle.py
```

这会下载模型(约 1.6GB)、所有依赖 wheel、ffmpeg 到包内,生成 ``manifest.json``。
打包完成后整个 ``Public/`` 目录拷到目标机,双击 ``launcher.exe`` 即可离线启动。

常用参数:

```powershell
python build_bundle.py --skip-model            # 跳过模型下载
python build_bundle.py --ffmpeg-zip D:\ff.zip  # 用本地压缩包(离线)
python build_bundle.py --repair                # 自动修复缺失组件
python build_bundle.py --check                 # 仅体检,不下载
```

## 目录结构

```
Public/
├─ launcher.exe                 # ★ 双击即用
├─ app/                         # 主工程源码(≈ 50 个文件,入库)
├─ config/                      # 关键词/评分 YAML 配置(入库)
├─ pyproject.toml               # Python 项目配置(入库)
│
├─ models/whisper-large-v3-turbo/   # 包内 Whisper 模型(约 1.6GB,build_bundle.py 下载)
├─ bin/                         # 包内 ffmpeg.exe / ffprobe.exe(build_bundle.py 下载)
├─ vendor/wheels/               # 全部依赖的离线 wheel(build_bundle.py 下载)
├─ .venv/                       # 虚拟环境(launcher.exe 自动创建)
├─ storage/                     # 运行产物(录制/切片/数据库/日志,gitignore)
├─ .env                         # 预置配置(已锁定包内模型)
│
├─ requirements-bundle.txt      # 运行时依赖清单
├─ build_bundle.py              # 一键打包(下载模型/依赖/ffmpeg)
├─ launcher.py / build_exe.py   # 启动器源码 + 编译脚本
├─ README_MAIN.md               # 主工程 README(参考)
└─ manifest.json                # 体检生成的自校验清单
```

## 分发前体检

打包完成后(或分发前)可运行体检核对模型、依赖、源码是否齐全:

```powershell
python build_bundle.py --check
```

体检报告覆盖:平台一致性、模型文件、FFmpeg、wheel 数量与顶层依赖、源码配置。

## 说明与前提

- **Whisper 固定为包内 large-v3-turbo**:``launcher.exe`` 自动以绝对路径注入,确保始终用包内模型。
- **FFmpeg**:``launcher.exe`` 自动下载到 ``bin/`` 并注入 PATH,**无需系统另装**。
  如 ffmpeg 已装好也可跳过下载。
- **大模型（可选）**:高光复核/文案/网感为可选增强;不配 ``LLM_API_KEY`` 时自动走纯规则,
  零费用可用。可在 Web「模型」页配置多个服务商并设优先级,失败自动回退。
- **CPU/GPU**:默认 ``cpu + int8``;有 NVIDIA 显卡可在 ``.env`` 改
  ``WHISPER_DEVICE=cuda`` + ``WHISPER_COMPUTE_TYPE=float16`` 提速。
- **平台一致性**:``vendor/wheels`` 与打包机器的操作系统/Python 版本相关,
  建议在与目标机器一致的环境（如 Windows x64 + Python 3.12）上打包。
