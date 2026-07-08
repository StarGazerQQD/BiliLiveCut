# BiliLiveCut · 即插即用版（`packaging/portable/`，原 Publish-PnP）

**版本：V0.1.14.5 Alpha** (`0.1.14.5-alpha`)

BiliLiveCut 是一个**全自动 AI 直播切片系统**：监听 Bilibili 直播间 → 实时录制 + 转写 → 识别高光爆点 → 生成剪辑成品 + 文案。

这个 `packaging/portable/` 目录是**即插即用分发版**。`launcher.exe` 内嵌了**固定版本**的完整业务源码 (Commit `74c21b4`，约 426KB Payload)，**双击即用，首次启动完全不访问 GitHub**。目标电脑只需装好 Python 3.11+，双击 `launcher.exe` 即可。

> **与旧版的关键区别**：旧版 PnP 首次启动从 GitHub 下载 `main` 分支源码（不稳定）。新版源码从 **EXE 内置 Payload** 释放，版本固定、离线可用、SHA-256 可校验。

---

## 系统要求

| 项目 | 最低 | 推荐 |
|------|------|------|
| 操作系统 | Windows 10/11 x64 | Windows 11 |
| Python | 3.11+ | 3.12 / 3.13 |
| 内存 | 8 GB | 16 GB |
| 磁盘 | 10 GB 空闲 | 20 GB（含录制存储） |
| 网络（Lite） | 首次需联网安装约 2 GB 依赖和模型 | — |
| 网络（Full） | **不需要** — 完全离线 | — |
| C 编译器（可选） | — | Visual Studio Build Tools（用于编译加速模块，无编译器则自动回退纯 Python） |

---

## V0.1.14.5 新特性：Portable 内嵌 Payload 构建系统

本次为**架构级改造**，建立从固定 Git Commit 提取源码、嵌入 Portable EXE 的完整离线发行链路。

### 核心变化

| 旧版 (Publish-PnP) | 新版 (packaging/portable) |
|---|---|
| 首次运行从 GitHub 下载 `main` 分支源码 | 首次运行从 **EXE 内置 Payload** 释放固定版本源码 |
| 源码版本不确定（随 `main` 漂移） | 源码固定于 `74c21b4`，SHA-256 可校验 |
| 无 Manifest / 无法校验完整性 | 完整 `payload_manifest.json` 含逐文件 SHA-256 |
| 无版本 Overlay 机制 | 受控 Release Metadata Overlay (仅 6 个文件可修改) |
| 无 Runtime 原子安装 | `staging → rename` 原子切换 + `current.json` 原子更新 |
| 单发行模式 | **Lite** (单 EXE) / **Full** (完全离线) 双发行 |

### Lite vs Full

| | Portable Lite | Portable Full |
|---|---|---|
| **大小** | ~10 MB (EXE) | ~3 GB (ZIP) |
| **内含** | EXE + Payload | EXE + Portable Python + Wheels + FFmpeg |
| **首次运行** | 需联网装依赖/模型 | **完全离线**，网络请求 0 |
| **适用场景** | 有网的一次性部署 | 断网环境 / 批量分发 |

---

## V0.1.14 新特性：架构重构 + 稳定性收口

### 模块化拆分 (Phase C1-C8)

> 将巨型单体拆分为可维护的子模块，为后续功能迭代扫清技术债。

- **task_worker.py** (1667行 → 5 个子模块): `lifecycle.py` / `stage_result.py` / `workers/` 下各阶段独立 compute/commit
- **CLI**: 拆分为 `app/commands/` 下 record/serve/doctor/config/room 等子命令
- **Web**: 拆分为 `web/services/` 12 个子服务 + `web/routers/` 子路由
- **DB 模型**: 拆分为 `app/db/entities/` 9 个实体子文件
- **加速模块**: C/Cython/Rust 统一归入 `app/accelerators/`

### 全链路崩溃安全 (Stability Closure)

> 本届聚焦"远端结果不丢失"和"进程崩溃后状态可恢复"。

- **Durable Journal**: 数据库不可用时将远程上传成功写入 JSONL 持久化日志
- **异常分类**: `classify_upload_error` 精确区分可重试/不确定/永久失败，不确定时不自动重试
- **Stale Recovery**: `full_recovery()` 全量恢复统一入口，超时 Attempt → `RECONCILIATION_REQUIRED`
- **308/308 全量测试通过**，Ruff 零错误

---

## V0.1.13 新特性：运行时集成与 Golden Path

> 稳定性组件从"代码中存在"提升到"真实接入主运行链路，并在故障下证明有效"。

- **TaskLease + Compute/Commit 分离**: 4 阶段全部拆分为纯计算 + 原子提交，租约贯穿全链路
- **ResourceBudget 资源预算**: CPU/GPU/内存/显存四维资源池，任务领取前 reserve
- **两级磁盘保护**: LOW(20GB) 暂停新任务，CRITICAL(5GB) 安全停止录制
- **FFmpeg 错误分类**: 瞬时网络 → 指数退避重试；磁盘满/权限 → 永久失败
- **Bilibili 风控熔断**: `CircuitBreaker` 房间级熔断，403/412 触发后退避
- **弹幕分级采样**: SC/互动 100% 采集，普通 30%，高密度 10%
- **Schema v1 系统**: 轻量 schema_meta 元信息表 + SHA-256 指纹，不兼容数据库拒绝启动
- **`bililivecut doctor`**: 15 项自检命令 (PASS/WARN/FAIL)
- **CI 增强**: pip-audit + pytest-cov 覆盖率门禁 + macOS 矩阵
- **290/290 测试通过**

---

## V0.1.12 新特性：多引擎 ASR 流水线

默认引擎从 Whisper 单引擎升级为四层流水线（全部模型首次运行时自动下载）：

| 层级 | 引擎 | 功能 |
|------|------|------|
| 主引擎 | Paraformer-zh | 中文文本、词级时间戳、标点 |
| 辅助特征 | SenseVoice-Small | 情感、笑声、音乐、事件检测 |
| 低置信复核 | Fun-ASR-Nano | 低分 / 非中文片段复核 |
| 最终兜底 | Whisper large-v3 / turbo | 主引擎失败时自动回退 |

通过 `.env` 中 `ASR_PRIMARY=whisper` 可切回纯 Whisper 模式。详见 `.env.example`。

---

## V0.1.11 新特性：数据一致性与流水线稳定性

- 五开关独立生效（`auto_record` / `auto_analyze` / `auto_render` / `auto_approve` / `auto_upload`），阶段转换时实时读取配置
- TaskWorker 真正并发 + 原子任务领取（防多 Worker 抢同一任务）
- 任务心跳 + stale 恢复 + `failed_stage` 精确重试

---

## V0.1.9 / V0.1.10 新特性：C / Cython / Rust 加速模块

自 V0.1.9 起，核心 CPU 热点使用多语言加速（自动检测 → 编译 → 回退纯 Python）：

- **Aho-Corasick 多模式匹配** 20–50×（C）
- **余弦相似度 / 字符 bigram** 3–8×（C）
- **聚类矩阵 O(N²)** 5–15× 纯 Python / 30–80× Rust+rayon 并行
- **弹幕基线分桶 + 中位数** 10–30×（Cython）
- **SRT 字幕组装** 3–8×（Cython）

- **C 扩展编译**: 安装 [Visual Studio Build Tools](https://visualstudio.microsoft.com/zh-hans/downloads/)（勾选「C++ 桌面开发」），然后:
  ```powershell
  python setup_c.py build_ext --inplace
  ```
- **Rust 编译 (可选)**: 安装 [Rust](https://rustup.rs) 后运行 `python build_rust.py`
- **启动确认**: 查看日志确认后端 — `加速模块(cluster): Rust+rayon 已加载` / `Cython 已加载` / `使用纯 Python 后备`

---

## 快速开始（零门槛）

### 方式一：双击 `launcher.exe`（推荐，有网 → Portable Lite）

1. 确保电脑已装 **Python 3.11+**（终端输入 `python --version` 检查）
2. 双击 `BiliLiveCut-Portable-Lite-*.exe`
3. 等待自动部署（首次约 10-30 分钟，视网速而定）：

| 步骤 | 内容 | 大小 | 说明 |
|------|------|------|------|
| ① | 释放源码 Payload | ~426 KB | 从 EXE 内置 Payload 释放 `app/` `config/` `pyproject.toml` `setup.py` 等，**无需 GitHub** |
| ② | 创建虚拟环境 | — | `.venv` 隔离 Python 依赖 |
| ③ | 安装依赖 | ~500 MB | 阿里云镜像 + 清华镜像（备用） |
| ④ | 下载 Whisper 模型 | ~1.6 GB | large-v3-turbo（hf-mirror.com），最耗时 |
| ⑤ | 下载 FFmpeg | ~80 MB | 无须系统另装 |
| ⑥ | 生成 `.env` 配置 | — | 含合理默认值 |

> **断点续跑**：任何一步失败或中断，再次双击自动从断点继续。
> **源码固定**：源码来源固定为 Commit `74c21b4`，不急-随 GitHub 上游变动。

4. 部署完成后自动打开浏览器 → **Web 管理控制台**（默认 `http://127.0.0.1:8000`）

> **Web 认证**：如需保护管理后台，在 `.env` 中设置 `ADMIN_PASSWORD=你的密码`。所有 API 操作将要求输入 Basic Auth（用户名固定为 `admin`）。

### 方式二：Portable Full 离线包（给无网机器）

1. 拿到 `BiliLiveCut-Portable-Full-*.zip`
2. 解压到任意目录
3. 双击 `BiliLiveCut-Portable.exe`
4. 启动器自动检测同目录下的 `portable-python/`、`vendor/wheels/`、`bin/ffmpeg.exe`
5. 完全离线安装 → 启动 Web

### 方式三：开发者手动打包

在一台**能联网**的机器上执行：

```powershell
cd packaging\portable
pip install huggingface_hub
python build_bundle.py
```

打包完成后把整个 `packaging/portable/` 目录拷到目标机，双击 `launcher.exe` 即可**离线启动**。

---

## 目录结构

```
packaging/portable/                     # ★ 即插即用分发版根目录 (原 Publish-PnP)
│
├── launcher.exe                     # ★★ 核心入口：双击即用，从内置 Payload 释放源码 + 部署
├── launcher.py                      # launcher.exe 的 Python 源码（可选，便于审查）
├── build_exe.py                     # Lite 版构建 (PyInstaller one-file)
├── build_full_bundle.py             # Full 离线包构建脚本
├── build_payload.py                 # Payload 构建器 (74c21b4 → source_payload.zip)
├── build_bundle.py                  # 兼容旧版离线打包（保留）
├── portable_launcher.spec           # PyInstaller 规格文件
├── pip.ini                          # pip 镜像源配置（阿里云 + 清华备用）
├── .env.example                     # 配置模板（launcher.exe 自动生成 .env）
├── .gitignore                       # Git 忽略规则
├── tests/                           # 19 项 Portable 专项测试
├── build/                           # 构建临时文件（gitignore 忽略）
├── dist/                            # 构建产物
│   ├── payload/                     #   source_payload.zip + manifest
│   ├── lite/                        #   Lite EXE
│   └── full/                        #   Full ZIP
└── README.md                        # 本文件
```

> **源码去哪了？** `app/` `config/` `pyproject.toml` 等业务文件**不在分发目录中**，而是内嵌在 `launcher.exe` 内部作为 **source_payload.zip**（从 Commit `74c21b4` 提取，固定版本，SHA-256 可校验）。`launcher.exe` 首次运行时自动将 Payload 解压到 `runtime/releases/` 目录。PnP 目录始终保持最小体积，源码不受 GitHub 主仓库 `main` 分支变动影响。

### 运行时动态生成（首次启动后）

```
├── runtime/                  # ★ Runtime 版本管理
│   ├── current.json          #   当前激活的 Release 信息
│   └── releases/
│       └── 0.1.14.5-alpha+74c21b4/  # Payload 释放的固定版本源码
│
├── .venv/                    # Python 虚拟环境（launcher.exe 自动创建）
├── models/                   # Whisper 语音模型（首次运行下载，约 1.6 GB）
│   └── whisper-large-v3-turbo/
├── bin/                      # ffmpeg.exe / ffprobe.exe（首次运行下载，约 80 MB）
│
├── vendor/wheels/            # 离线依赖 wheel（build_bundle.py 构建时下载）
├── data/                     # 数据库
├── storage/                  # ★ 运行产物目录
│   ├── raw/                  #   原始录制片段
│   ├── clips/                #   成品切片
│   └── ready_to_upload/      #   待上传清单
└── .env                      # 用户配置文件（首次运行自动生成）
```

---

## Web 控制台各功能页说明

启动后在浏览器中打开的 Web 控制台包含以下 Tab：

| Tab | 功能 | 前置条件 |
|-----|------|----------|
| **直播间** | 添加房间号、开关录制 | 无 |
| **录制状态** | 当前录制进度、断流/重连状态 | 已添加房间 |
| **录制预约** | 定时自动录制（每日/单次） | 已添加房间 |
| **实时转写** | 多引擎 ASR 自动转录当前片段 | 录制进行中 |
| **弹幕热度** | 弹幕实时统计、热度曲线 | 已配置 Bilibili Cookie |
| **网感资料库** | 联网采集热点话题/标签 | 已配置大模型 API |
| **候选审核** | 高光片段候选列表、横屏审片工作台 | 已完成分析 |
| **成品切片** | 已剪辑的视频、封面、文案（含多版本变体） | 已生成切片 |
| **主题管理** | 高光话题聚合、合集编辑 | 已有候选数据 |
| **上传 / 设置** | 上传开关、自动化开关 | — |
| **模型** | 多 LLM 服务商配置、优先级 | — |
| **账号管理** | 扫码登录 Bilibili、自动采集 Cookie | — |
| **运维面板** | 任务队列监控、Worker 状态、失败重试 | — |
| **错误日志** | WARNING/ERROR 级别日志 | — |

---

## 配置文件 .env 详解

`launcher.exe` 首次运行会**自动生成**一份含默认值的 `.env`。以下逐段说明每个配置项的含义及何时需要修改。

### 通用与管理认证

```ini
APP_ENV=dev          # dev=开发(更多日志) / prod=生产
LOG_LEVEL=INFO       # DEBUG / INFO / WARNING / ERROR
ADMIN_PASSWORD=      # ★ Web 管理后台密码（空则不启用认证，用户名固定 admin）
```

### 存储

```ini
STORAGE_ROOT=./storage          # 所有产物（录制片段、切片、数据库、日志）的根目录
DATABASE_URL=sqlite:///./storage/blc.db  # SQLite 数据库路径，一般不改
```

### FFmpeg

```ini
FFMPEG_PATH=ffmpeg    # launcher.exe 会自动下载到 bin/ 并注入 PATH
FFPROBE_PATH=ffprobe  # 如系统已装好的 FFmpeg 在 PATH 中，保持默认即可
```

### 录制 / 分片

```ini
SEGMENT_DURATION_S=60            # 每个原始片段的时长（秒），建议 60
PREFERRED_STREAM_PROTOCOL=hls    # 取流协议：hls（稳定）或 flv
STREAM_QUALITY=10000             # 清晰度：10000=原画，400=蓝光，250=超清
RECONNECT_MAX_BACKOFF_S=30       # 断流后最大重试等待秒数
LIVE_POLL_INTERVAL_S=15          # 检查直播间开播/下播的间隔（秒）
COLLECT_DANMAKU=true             # 录制时是否同时采集弹幕（需 Bilibili Cookie）
```

### Bilibili Cookie（弹幕采集必需）

```ini
REQUIRE_AUTHORIZATION=true  # 必须设为 true，确认你有权录制
BILIBILI_COOKIE=            # 登录态 Cookie，填写后可采集弹幕、获取更高清晰度
                            # 推荐通过 Dashboard【账号管理】Tab 扫码登录自动获取
                            # 手动填写格式：DedeUserID=xxx; SESSDATA=xxx; bili_jct=xxx
```

> **重要**：不填 Cookie 时弹幕采集不可用（B 站接口要求登录态），但录制本身不受影响。

### ASR 语音转写（本地多引擎，无需联网）

```ini
# 主引擎（V0.1.12）
ASR_PRIMARY=paraformer                  # paraformer=四层流水线（推荐）/ whisper=纯 Whisper 模式
ASR_WHISPER_MODEL=large-v3-turbo        # Whisper 模型大小（仅 ASR_PRIMARY=whisper 或兜底时使用）
ASR_DEVICE=cpu                          # cpu=通用 / cuda=NVIDIA GPU
ASR_COMPUTE_TYPE=int8                   # int8 (CPU) / float16 (GPU)
# 辅助层
ASR_SENSEVOICE=true                     # 情感/笑声/音乐/事件检测（需 funasr + modelscope）
ASR_FUNASR_REVIEW=true                  # 低置信片段复核（需 funasr + modelscope）
ASR_FALLBACK_WHISPER=true               # 主引擎失败时自动回退 Whisper
ASR_CONFIDENCE_THRESHOLD=0.85           # 低于此置信度的片段触发复核
ASR_MODEL_REVISION=v2.0.4               # 模型版本锁定
```

### 大模型（可选，用于高光复核 / 文案 / 网感采集）

```ini
LLM_PROVIDER=deepseek                # 仅标识，不影响实际连接
LLM_API_KEY=                         # ★ 填入 API Key 才启用大模型；留空走纯规则（零费用）
LLM_BASE_URL=https://api.deepseek.com/v1   # 服务商 base_url，须含版本前缀
LLM_MODEL=deepseek-chat              # 模型标识名
LLM_WEB_SEARCH_PARAM=enable_search   # 联网搜索开关键名（DeepSeek 不支持则自动回退）
LLM_PRICE_INPUT_PER_M=0              # 每百万 token 输入价格（0=不计费）
LLM_PRICE_OUTPUT_PER_M=0             # 每百万 token 输出价格（0=不计费）
LLM_DAILY_BUDGET=0                   # 每日预算上限（0=不限）
```

**多模型配置**：Web 控制台「模型」Tab 可同时添加多个服务商（DeepSeek / 通义千问 / Kimi / 智谱 GLM 等），设置优先级，某个不可用时自动降级到下一个。

### 网感资料库（可选，用于热点采集）

```ini
TREND_ENABLED=false              # 是否启用网感资料库
TREND_API_KEY=                   # 趋势采集专用 API Key（可与 LLM_API_KEY 不同）
TREND_BASE_URL=                  # 趋势采集专用 base_url（留空复用 LLM_BASE_URL）
TREND_MODEL=                     # 趋势采集专用模型（留空复用 LLM_MODEL）
TREND_WEB_SEARCH=true            # 是否开启联网搜索（时效性强）
TREND_MAX_SEARCHES=5             # 单次采集联网搜索次数上限
TREND_MAX_ITEMS=40               # 单次采集入库条目上限
TREND_RETENTION_DAYS=14          # 资料库保留天数
TREND_MATCH_DAYS=7               # 高光评分参考"近期"窗口（天）
```

> 启用后可在「网感资料库」Tab 手动触发采集，或开启「定时采集」每日自动迭代。

### 高光判断阈值

```ini
HIGHLIGHT_INIT_THRESHOLD=0.5     # 进入 LLM 复核的初筛阈值（0-1）
HIGHLIGHT_THRESHOLD=0.65         # 进入候选池的综合评分阈值（0-1）
AUTO_PUBLISH_THRESHOLD=0.85      # 全自动模式下直接发布的阈值（0-1）
```

> 阈值越低越容易切片（更多候选但可能含低质），越高越严格（少而精）。

### 切片后处理

```ini
CLIP_LOUDNORM=true               # 响度标准化（EBU R128），推荐开启
CLIP_REMOVE_SILENCE=false        # 去除首尾静默段
CLIP_VERTICAL=false              # 竖屏重构 1080x1920（适合手机端）
CLIP_SUBTITLE=false              # 烧录字幕（从转写生成 SRT）
CLIP_MAX_DURATION_S=180          # 单个切片最大时长（秒）
CLIP_VIDEO_CRF=20                # x264 画质（0-51，越小越清晰）
CLIP_PRESET=veryfast             # 编码速度：ultrafast / veryfast / medium / slow
```

### 上传

```ini
UPLOADER=manual                  # manual=仅产出文件不投稿（零风险，推荐）
UPLOAD_MAX_RETRIES=3             # 上传失败重试次数
UPLOAD_MAX_PER_HOUR=5            # 每小时投稿上限（频控）
TITLE_MAX_LEN=80                 # 标题长度上限
DESC_MAX_LEN=2000                # 简介长度上限
```

### biliup 上传（社区方案，合规风险自负）

```ini
BILIUP_CONFIG=                              # biliup 配置文件路径（如 cookies.json）
BILIUP_UPLOAD_CMD=                          # 自定义上传命令模板
# 例：BILIUP_UPLOAD_CMD=biliup upload "{file}" --title "{title}" --desc "{desc}"
```

> ⚠ **B 站无面向普通用户的官方公开投稿 API**。biliup 使用你自己的登录态走网页投稿端点，可能违反平台条款、触发风控甚至封号。是否启用由你决定，风险自负。

---

## 首次使用完整流程

1. **双击 `launcher.exe`** → 等待自动部署完成（Lite 版首次联网下载约 10-30 分钟；Full 版秒开）
2. 浏览器打开 Web 控制台 → **「账号管理」Tab** → 点击「登录」→ 扫码获取 Cookie
3. **「直播间」Tab** → 粘贴直播间链接（如 `https://live.bilibili.com/123456`）→ 勾选授权确认 → 添加
4. 等待主播开播 → 系统**自动检测并开始录制 + 转写**
5. 录制结束后自动分析 → 在 **「候选审核」Tab** 审批高光片段
6. 审批通过后自动生成剪辑 → 在 **「成品切片」Tab** 查看
7. 成品（MP4 + 封面 + 文案）输出在 `storage/clips/` 目录

> **可选增强**：在 `.env` 配置 `LLM_API_KEY` 后，高光复核和文案生成将由大模型辅助（否则走纯规则，同样可用）。
> **安全建议**：多人共用或暴露在局域网时，设置 `ADMIN_PASSWORD` 启用 Web 后台认证。

---

## 常用故障排查

| 问题 | 解决 |
|------|------|
| 启动报 `python` 不是命令 | 安装 Python 3.11+，安装时勾选「Add to PATH」 |
| Payload 释放失败 | 检查 EXE 完整性，SHA-256 不匹配时自动拒绝安装 |
| 下载模型卡住不动 | 关闭窗口重新双击，模型支持断点续传 |
| ASR 报 `funasr` 未安装 | `pip install funasr modelscope`（Paraformer/SenseVoice/FunASR 必需） |
| ASR 主引擎无法加载 | 检查安装了 modelscope (`pip install modelscope`)，首次需下载模型 |
| 弹幕采集提示 `code=-352` | 未配置 Bilibili Cookie，去「账号管理」Tab 扫码登录 |
| LLM 调用报错 / 空结果 | 检查 `.env` 中 `LLM_API_KEY` 和 `LLM_BASE_URL` 是否正确 |
| FFmpeg 未找到 | 重新运行 `launcher.exe`，它会检测并自动补充下载 |
| API 请求返回 401 | `.env` 中设置了 `ADMIN_PASSWORD`，浏览器需要输入用户名 `admin` + 密码 |
| 修改 `.env` 后不生效 | 重启 `launcher.exe` |
| `launcher.exe` 被杀软拦截 | 添加信任白名单 |
| Runtime 损坏 | 删除 `runtime/` 目录后重新启动，Launcher 会自动重新安装 |

---

## 回主工程

此 `packaging/portable/` 目录是**发布给最终用户的即插即用版本**，源码固定于 Commit `74c21b4` (v0.1.14.5-alpha)。

- **主仓库**: `D:\Vibe\BiliLiveCut\README.md`
- **完整变更日志**: `D:\Vibe\BiliLiveCut\CHANGELOG.md`
- **Portable 构建文档**: 本文件及 `packaging/portable/` 下的 Python 模块
