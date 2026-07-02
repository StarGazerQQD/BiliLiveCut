# BiliLiveCut · 即插即用版(Public)

**版本:V0.1.1 Alpha** (`0.1.1-alpha`)

一个**自包含**的分发版:把「Whisper 模型 + 全部外部依赖 + 源码」封装在本目录内,
目标机器**无需联网**即可运行。语音转写**固定使用包内的 Whisper `large-v3-turbo`**。

## 最简单的方式:双击 .exe(推荐,无需 .ps1/.bat)

打包完成后,直接**双击 `launcher.exe`** 即可:

1. 自动检测 Python 环境(需 Python 3.11+)
2. 自动创建虚拟环境 + 离线安装依赖
3. 自动启动 Web 管理后台
4. 自动打开浏览器访问 http://127.0.0.1:8000

> 全程无需 .ps1 / .bat 脚本,不受系统安全策略拦截。首次启动会安装依赖(1-3 分钟),之后秒开。

## 目录结构(打包完成后)

```
Public/
├─ launcher.exe                 # ★ 双击即用(无需脚本),含 build_exe.py
├─ app/                         # 主工程源码副本(由 build_bundle.py 复制)
├─ config/                      # 关键词/评分等配置副本
├─ models/whisper-large-v3-turbo/   # 包内 Whisper 模型(约 1.6GB)
├─ bin/                         # 包内 ffmpeg.exe / ffprobe.exe(免另装)
├─ vendor/wheels/               # 全部依赖的离线 wheel(封装的外部库)
├─ .venv/                       # 初次运行时自动创建(动态生成)
├─ storage/                     # 运行产物(录制/切片/数据库/日志)
├─ .env                         # 预置配置(已锁定包内模型)
├─ requirements-bundle.txt      # 运行时依赖清单
├─ build_bundle.py              # 一键打包(联网机器执行一次)
├─ launcher.py / build_exe.py   # 启动器源码(重新编译所用)
├─ install.ps1 / install.bat    # 离线安装依赖(备用)
├─ run.ps1 / run.bat            # 启动 Web 后台(备用)
├─ setup.ps1 / setup.bat        # 自动修复+安装+启动(备用)
├─ check.ps1 / check.bat        # 分发前一键体检
├─ manifest.json                # 体检生成的自校验清单(打包后自动产出)
└─ README_MAIN.md               # 主工程 README(参考)
```

## 备用:脚本方式(如遇到 .exe 问题)

拿到 `Public/` 后,直接双击 **`setup.bat`**(或 `./setup.ps1`)即可:

```
setup = 自动修复(按需下载模型/依赖/ffmpeg,平台不一致则按本机重下)→ 离线安装 → 启动
```

它会**自动处理问题而非只提示**:

- **平台不一致**(比如把在别的系统/Python 版本打的包拿到本机):自动清掉错平台的 wheel 与
  ffmpeg,并**按当前机器**重新下载合适的 Python 依赖与 ffmpeg;
- **缺 wheel / 缺组件**:自动 `pip download` 补齐(默认走清华镜像);
- **缺模型**:自动下载 `large-v3-turbo`(默认走 `hf-mirror.com`);
- **缺 ffmpeg**:自动下载;下载失败会尝试从系统 PATH 复制。

> 自动修复需要联网。若目标机器完全离线,请在**打包机**上先把包做全(见下),
> 且打包机与目标机的操作系统 / CPU 架构 / Python 次版本一致。
> 只想修复不启动:`python build_bundle.py --repair`。

## 三步使用(手动/进阶)

### 1)打包(仅需一次,在**能联网**的机器)

```powershell
# 需要已装 Python 3.11/3.12 与 huggingface_hub
pip install huggingface_hub
python build_bundle.py
```

> pip 已内置两条国内镜像:**清华(主)+ 阿里云(备选 extra-index)**,封装依赖时自动走这两条快链路
> (见 `pip.ini`;setup/install 脚本通过 `PIP_CONFIG_FILE` 全程启用)。如需换主镜像:
> `python build_bundle.py --pip-index <你的镜像>`(阿里云/清华仍作为备选附加)。

这会:下载 `large-v3-turbo` 模型(默认走 `hf-mirror.com`)、把全部依赖下成 wheel、
**整合 ffmpeg/ffprobe 到 `bin/`**、复制源码,**并在最后自动跑一次体检**、生成 `manifest.json`。

常用参数:

```powershell
python build_bundle.py --ffmpeg-zip D:\ffmpeg-win64.zip   # 用本地 ffmpeg 压缩包(离线)
python build_bundle.py --skip-ffmpeg                       # 不整合 ffmpeg(改用系统 ffmpeg)
python build_bundle.py --skip-model                        # 跳过模型下载
```

### 2)离线安装依赖(目标机器)

```powershell
./install.ps1      # 或双击 install.bat
```

### 3)启动

```powershell
./run.ps1          # 或双击 run.bat
```

浏览器打开 http://127.0.0.1:8000 即为管理后台。

## 分发前一键体检

打包完成后(或分发前的任意时刻)运行体检,核对模型、依赖、源码是否齐全:

```powershell
./check.ps1        # 或双击 check.bat;等价于 python build_bundle.py --check
```

体检会检查并输出报告(同时刷新 `manifest.json`):

- **平台一致性**:对比 `manifest.json` 记录的**打包平台**与当前机器的
  操作系统 / CPU 架构 / Python 次版本;任一不一致会告警(如把 Linux 的 wheel
  拿到 Windows、或 py3.10 打包却在 py3.14 安装,离线安装必然失败);
- **模型**:`models/whisper-large-v3-turbo/model.bin` 是否存在且非空,`config.json`、
  `tokenizer.json` 等必需文件是否齐全;
- **FFmpeg**:`bin/` 下 `ffmpeg`、`ffprobe` 是否就位;
- **依赖 wheel**:`vendor/wheels` 中 wheel 数量,以及 `requirements-bundle.txt` 的
  **顶层依赖是否全部封装**;并对关键(含原生)传递依赖(ctranslate2 / tokenizers /
  onnxruntime / av / starlette 等)缺失给出告警;
- **源码/配置**:`app/`、`config/`、`pyproject.toml` 是否就位;`.env` 是否存在。

全部通过时进程退出码为 `0`(报告显示「[PASS] 可分发」),否则为 `1` 并逐条列出问题——
可直接用于分发前的自动化把关(CI/脚本)。

> 平台校验依据打包时写入 `manifest.json` 的 `build_platform`;`--check` 只读取该记录做对比、
> **不会覆盖**它,因此把整个 `Public/` 拷到目标机再体检即可发现平台不匹配。

## 说明与前提

- **Whisper 固定为包内 large-v3-turbo**:由 `.env` 的 `WHISPER_MODEL` 指向
  `./models/whisper-large-v3-turbo`,run 脚本再以绝对路径注入,确保始终用包内模型、不联网下载。
- **FFmpeg**:已整合进包内 `bin/`(打包时下载 Windows 静态构建)。run 脚本会自动
  以 `bin/ffmpeg.exe`、`bin/ffprobe.exe` 注入并加入 PATH,**无需系统另装**。
  如用 `--skip-ffmpeg` 打包,则回退使用系统 `ffmpeg`(需自行装好)。
- **大模型(可选)**:高光复核/文案/网感为可选增强;不配 `LLM_API_KEY` 时自动走纯规则,
  零费用可用。可在 Web「模型」页配置多个服务商并设优先级,失败自动回退。
- **CPU/GPU**:默认 `cpu + int8`;有 NVIDIA 显卡可在 `.env` 改
  `WHISPER_DEVICE=cuda` + `WHISPER_COMPUTE_TYPE=float16` 提速。
- **平台一致性**:`vendor/wheels` 与打包机器的操作系统/Python 版本相关,
  建议在与目标机器一致的环境(如 Windows x64 + Python 3.12)上打包。
