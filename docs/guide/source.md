# 源码部署

在 Windows、macOS 或 Linux 上，可以直接安装并运行 BiliLiveCut 的 Python 源码。下面提供 uv 和传统 venv + pip 两种方式，选择其中一种即可。业务控制台使用独立 React 前端，构建时需要 Node.js 24，运行构建产物不需要 Node.js。

## 准备运行环境

- **Python**：优先使用 3.12，也可使用 3.11；其他版本是否可用还取决于所选依赖的支持情况。
- **FFmpeg 与 FFprobe**：将真实可执行文件加入 PATH，或在 `.env` 中设置 `FFMPEG_PATH`、`FFPROBE_PATH`。字幕与片头渲染需要 FFmpeg 包含 `subtitles`、`drawtext` 滤镜，并准备可用的中文字体。
- **编译工具与 Python 开发头文件**：默认源码安装会编译 C/Cython/C++ 扩展。Windows 使用 MSVC，macOS 使用 Command Line Tools，Linux 使用对应编译器及 Python 开发包。缺少头文件或需要纯 Python 安装时，见[原生扩展安装排错](../development/environment.md#原生扩展构建与安装排错)。
- **Git 或源码压缩包**：获取完整项目文件，包括 `pyproject.toml`、`uv.lock`、`app/`、`config/` 和 `tools/` 等目录。

如果使用 Git，可在终端执行：

```bash
git clone https://github.com/StarGazerQQD/BiliLiveCut.git
cd BiliLiveCut
```

也可下载并解压源码，然后在解压后的项目根目录打开终端。后续所有命令均在项目根目录执行；示例中的 `python` 应指向所选 Python，部分系统在创建虚拟环境前需使用 `python3`。

检查 FFmpeg：

```bash
ffmpeg -hide_banner -version
ffprobe -hide_banner -version
ffmpeg -hide_banner -filters
```

## 安装方式一：uv

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后执行：

```bash
uv sync --locked --no-dev --extra web
```

uv 会按仓库锁文件创建 `.venv` 并安装项目，无需手动激活。该命令先安装 Web 管理和录制所需依赖，不安装 ASR、LLM 可选依赖；需要转写和分析时见下文。

后续使用 `uv run --no-sync` 运行当前环境，避免因省略 extras 而移除已安装的可选依赖。更新依赖时再显式执行带有所需 extras 的 `uv sync`。

## 安装方式二：venv + pip

使用系统已安装的 Python 创建虚拟环境：

```bash
python -m venv .venv
```

按终端类型激活环境：

| 终端 | 激活命令 |
| --- | --- |
| Windows PowerShell | `.\.venv\Scripts\Activate.ps1` |
| Windows CMD | `.venv\Scripts\activate.bat` |
| macOS / Linux 的 bash 或 zsh | `source .venv/bin/activate` |

激活后安装：

```bash
python -m pip install -e ".[web]"
```

如果 PowerShell 不允许执行激活脚本，可直接使用 `.\.venv\Scripts\python.exe` 替代后续命令中的 `python`，使用 `.\.venv\Scripts\blc.exe` 替代 `blc`，无需激活。pip 根据 `pyproject.toml` 安装依赖，不使用 `uv.lock`。

## 按需安装转写与分析依赖

需要全部本地 ASR 后端和 LLM 支持时，将前面的安装命令换为以下对应命令；已完成基础安装也可执行以补齐依赖：

```bash
# uv
uv sync --locked --no-dev --extra web --extra asr-all --extra llm
```

```bash
# 已激活的 venv + pip
python -m pip install -e ".[web,asr-all,llm]"
```

依赖安装与模型准备是两步。实际转写还需所选引擎的模型及匹配的推理依赖；仅安装以上 extras 不代表模型、PyTorch 或 CUDA 运行时已经全部准备好。按[语音转写与模型](../features/asr.md)配置，使用 CPU 时选择对应 CPU 设备；需要 GPU 时再按该指南核对运行时与实际设备。LLM 还需配置服务商与 API Key。

## 构建控制台

安装 Node.js 24，在仓库根目录执行：

```bash
npm --prefix frontend ci
npm --prefix frontend run build
```

静态产物位于 `frontend/dist/`。它与 Python 后端独立服务；使用下文的 `start` 命令即可同时启动两个进程。只需后端 API 时使用 `backend`，该命令不再返回网页。

## 配置与启动

项目安装后提供 `blc` 命令。后续文档中的 `blc ...` 示例，在 uv 环境下执行 `uv run --no-sync blc ...`；使用 venv + pip 时，激活环境后直接执行 `blc ...`。可用 `blc --help` 查看全部子命令，或用 `blc <子命令> --help` 查看参数。

将项目中的 `.env.example` 复制为 `.env`，已有文件时保留原配置。根据需要编辑：

- `ADMIN_PASSWORD`：管理员密码，用户名为 `admin`；远程部署必须设置非空密码。本机未设置密码时，仅允许满足本机访问约束的请求。
- `FFMPEG_PATH`、`FFPROBE_PATH`：未加入 PATH 时填写真实可执行文件路径。
- `STORAGE_ROOT`：按需设置数据目录，默认使用当前运行目录下的 `storage/`。

其他参数见[配置参考](configuration.md)。源码模式使用 HTTP Basic Auth，Bilibili 账号通过网页扫码单独登录，说明见[后台认证与 Bilibili 登录](login.md)。

uv 安装方式启动：

```bash
uv run --no-sync blc init
uv run --no-sync blc start --host 127.0.0.1 --port 8000
```

venv + pip 安装方式启动：

```bash
blc init
blc start --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。保持终端运行，停止服务时按一次 `Ctrl+C`，等待录制末片登记、弹幕连接和后台任务完成收尾。后台任务的等待时间由 `worker_shutdown_timeout_seconds` 控制；一键启动器会等待后端退出。再次按 `Ctrl+C` 会强制终止前后端及其子进程，可能中断正在写入的片段，应仅在正常关闭无法完成时使用。端口已被占用时，可将 `--port` 改为其他可用端口，并使用对应地址访问。

远程访问需先设置管理员密码，再配置监听地址或 SSH 隧道；Linux 无头部署的具体示例见[服务器指南](linux-server.md#从自己的电脑访问)。

## 第一次使用与后续更新

按[第一次完成工作流](quick-start.md)进行短时间录制，或[导入已有录播](../features/recording-import.md)，再验证转写、分析、审核与剪辑。Web 页面能打开不代表模型推理与完整链路已经验收。

再次启动时使用同一源码版本、配置和运行目录，通常只需运行 `start`。升级前保留旧环境，当前 Alpha 版本不直接迁移历史数据库；使用新目录初始化当前版本，具体见[升级说明](getting-started.md#升级前保留旧环境)。

## 使用 wheel 发行包

已构建的 `.whl` 自带 React 静态前端，安装与运行无需 Node.js，也不需要另行执行 npm 构建。准备与 wheel 标签匹配的 Python、FFmpeg 和虚拟环境，将以下文件名替换为实际下载的 wheel：

```bash
python -m pip install "./实际文件名.whl[web]"
blc init
blc start --host 127.0.0.1 --port 8000
```

从希望保存配置和数据的目录执行 `blc`；前端资源自动从已安装包读取。配置与数据目录规则和源码运行相同，需要转写与分析时安装相应 extras 并准备模型及服务。自行从源码或 sdist 构建 wheel 仍需要 Node.js 24，构建过程见[前端发行构建](../development/frontend.md#wheel-与源码包)。

## 避免重复启动同一数据库

同一个 SQLite 数据库只允许一个 Web 作业执行实例。若启动时提示「当前数据库已有 Web 作业执行实例」，先停止正在使用该数据库的旧实例，再启动新服务。需要独立运行多套服务时，为每套服务配置独立数据库和存储目录。

程序在数据库旁维护 `.web-jobs.lock` 锁文件；正常关闭或进程退出后由操作系统释放执行锁。锁文件仍存在不表示服务仍在运行，无需手动删除。停机超时、处理器尚未确认退出时，会继续持有锁，防止另一实例重复恢复正在执行的作业。
