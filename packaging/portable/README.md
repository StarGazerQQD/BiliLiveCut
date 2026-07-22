# BiliLiveCut · 即插即用版（`packaging/portable/`，原 Publish-PnP）

**版本：V0.1.15.1 Alpha** (`0.1.15.1-alpha`)

> **普通用户请先阅读：[Portable 小白使用说明](USER_GUIDE_ZH.md)**。该说明按 Windows 用户从下载、校验、解压、首次启动到第一次录制的顺序编写。

BiliLiveCut 是一个**全自动 AI 直播切片系统**：监听 Bilibili 直播间 → 实时录制 + 转写 → 识别高光爆点 → 生成剪辑成品 + 文案。

这个 `packaging/portable/` 目录是**即插即用分发版**。Launcher 内嵌了当前发布基线的完整业务源码 (Commit `4bdaa13`)，**双击即用，首次启动不需要从 GitHub 下载业务源码**。Full 版自带 Python 3.12、离线依赖和 FFmpeg；Lite 版需要目标电脑已有 Python 3.11/3.12，并自行满足 FFmpeg 等运行组件。

> **与旧版的关键区别**：旧版 PnP 首次启动从 GitHub 下载 `main` 分支源码（不稳定，且国内访问 GitHub 经常失败）。新版源码从 **EXE 内置 Payload** 释放，版本固定、SHA-256 可校验，彻底摆脱 GitHub 依赖。

---

## 系统要求

| 项目 | 最低 | 推荐 |
|------|------|------|
| 操作系统 | Windows 10/11 x64 | Windows 11 |
| Python | Full 无需系统 Python；Lite 需要 3.11 或 3.12 | 3.12 |
| 内存 | 8 GB | 16 GB |
| 磁盘 | 15 GB 空闲（仅短时测试） | 30 GB 以上（含模型和录制存储） |
| 网络（运行时） | **需要** — 录直播、采集弹幕、调大模型等均需联网 | — |
| 网络（安装） | **Lite**: 首次安装需联网拉取依赖；**Full**: 预置完整，组件安装无需额外下载；**模型**: 通过独立 **Engine Pack** 或在线下载安装；**账号登录**: 无系统 Chrome 时会下载 Chromium | — |
| C 编译器（可选） | — | Visual Studio Build Tools（用于编译加速模块，无编译器则自动回退纯 Python） |

---

## V0.1.14.6 新特性：发行结构重构

本次为**架构级改造**，建立从固定 Git Commit 提取源码、嵌入 Portable EXE 的完整发行链路。彻底解决国内从 GitHub 拉取源码不稳定的问题。

### 核心变化

| 旧版 (Publish-PnP) | 新版 (packaging/portable) |
|---|---|
| 首次运行从 GitHub 下载 `main` 分支源码 | 首次运行从 **EXE 内置 Payload** 释放固定版本源码 |
| 源码版本不确定（随 `main` 漂移） | 源码固定于当前发布基线 `4bdaa13`，SHA-256 可校验 |
| 无 Manifest / 无法校验完整性 | 完整 `payload_manifest.json` 含逐文件 SHA-256 |
| 无版本 Overlay 机制 | 受控 Release Metadata Overlay (仅 6 个文件可修改) |
| 无 Runtime 原子安装 | `staging → rename` 原子切换 + `current.json` 原子更新 |
| 单发行模式 | **Lite** (轻量化单 EXE) / **Full** (预置完整依赖) 双发行 |

### Lite vs Full

> 两种发行方式均面向**联网运行**场景（直播监-听、弹幕采集、大模型调用等均需网络）。区别仅在于**安装阶段是否需要额外从互联网下载组件**。

| | Portable Lite | Portable Full |
|---|---|---|
| **定位** | 轻量化，方便下载分发 | 完整包，开箱即用 |
| **当前发行大小** | ~40 MB (EXE) | ~1 GB (ZIP，解压后更大) |
| **内含** | EXE + 业务源码 Payload | EXE + Portable Python + Wheels + FFmpeg |
| **安装过程** | 首次安装时在线下载 Python 依赖；FFmpeg 需另行准备 | 预置 Python、依赖和 FFmpeg，组件安装无需额外下载 |
| **模型** | 不含模型，通过独立 **Engine Pack** 或在线下载安装 | 不含模型，通过独立 **Engine Pack** 或在线下载安装 |
| **适用场景** | 熟悉 Python/FFmpeg 的高级用户 | 普通用户、小规模分发测试 |

> **模型策略**: Lite 和 Full 均不携带四引擎 ASR 模型。模型统一由独立的 **Portable Engine Pack** 提供。将 Engine Pack ZIP 放在程序同级目录，首次启动时会安全解压并按内部 Manifest 逐文件校验 SHA-256；本地嵌入了正式元数据的构建还会校验外部 CRC32/SHA-256。无本地包时自动在线下载全部四个引擎模型。

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
- **Rust 编译 (可选)**: 安装 [Rust](https://rustup.rs) 后运行 `python tools/native/build_rust.py`
- **启动确认**: 查看日志确认后端 — `加速模块(cluster): Rust+rayon 已加载` / `Cython 已加载` / `使用纯 Python 后备`

---

---

## Portable Engine Pack — 独立四引擎模型包

Lite 和 Full 均不携带 ASR 模型。四个引擎模型统一由独立的 **Engine Pack** 提供。

### 四种引擎

| 引擎 | 模型 ID | 来源 | 版本 |
|------|---------|------|------|
| Whisper (兜底) | large-v3-turbo | HuggingFace (mobiuslabsgmbh/) | — |
| Paraformer-zh (主引擎) | paraformer-zh | ModelScope | v2.0.4 |
| SenseVoice-Small (辅助特征) | iic/SenseVoiceSmall | ModelScope | v2.0.4 |
| Fun-ASR-Nano (低置信复核) | iic/Fun-ASR-Nano | ModelScope | v2.0.4 |

> Paraformer 额外需要 smn-vad / ct-punc / cam++ 三个子模型 (自动下载)。

### 使用方式

1. 下载 BiliLiveCut-EnginePack-0.1.15.1-alpha.zip
2. 放在 Launcher EXE **同级目录** (或 packages/ 子目录)
3. 双击启动 Launcher → 自动 **CRC32 校验** → 校验通过即离线安装 (网络请求 0)
4. 无本地包或校验失败 → 自动**全量在线下载**四个引擎模型

### CRC32 校验

| 行为 | 说明 |
|------|------|
| CRC32 匹配 | 解压安装，**完全离线**，网络请求 → 0 |
| CRC32 不匹配 | 不使用包内任何模型，**全量在线下载**四个引擎 |
| 本地包缺失 | **全量在线下载**四个引擎 |
| 内部 Hash 失败 | 即便 CRC32 正确，也**全量在线下载** |

任何时候不允许混合本地包与在线模型。四个引擎必须作为一个整体安装。

### 模型安装目录

`
<程序根目录>/
├── BiliLiveCut-Portable.exe
├── models/
│   ├── whisper/                    # Whisper large-v3-turbo
│   ├── paraformer/                 # Paraformer-zh + vad/punc/speaker
│   ├── sensevoice/                 # SenseVoice-Small
│   ├── funasr_nano/                # Fun-ASR-Nano
│   └── engine-pack-installed.json  # 安装清单 (自动生成)
`

模型安装在程序 Portable 根目录的 models/ 下，不写入用户主目录缓存。

### 构建 Engine Pack

`powershell
cd packaging\portable
python build_engine_pack.py           # 真实下载四引擎模型并打包
python build_engine_pack.py --fixture # 生成测试用小型 Fixture
`

输出:
- dist/engine-pack/BiliLiveCut-EnginePack-0.1.15.1-alpha.zip
- dist/engine-pack/engine-pack-manifest.json
- dist/engine-pack/CRC32SUMS.txt
- dist/engine-pack/SHA256SUMS.txt

resources/engine_pack_info.json (本地 Engine Pack 构建后可供 Lite/Full EXE 嵌入；GitHub Release 不嵌入 fixture)

### 注意事项

- Engine Pack 预计体积 ≥ 4 GB，请使用 **NTFS** 或 **exFAT** 文件系统 (FAT32 不支持单文件 >4GB)
- CRC32 为 8 位大写十六进制，流式计算，不将整个文件读入内存
- SHA-256 用于发布文件校验和逐文件完整性验证
- 已安装模型有效时不会重复解压或下载
- 应用升级不会自动删除模型目录

## 快速开始（零门槛）

### 方式一：下载 Lite 版（高级用户，体积小）

1. 确保电脑已装 **Python 3.11 或 3.12**（终端输入 `python --version` 检查），并自行准备 FFmpeg/FFprobe
2. 双击 `BiliLiveCut-Portable-Lite-*.exe`
3. 等待自动部署（首次约 10-30 分钟，视网速而定）：

| 步骤 | 内容 | 大小 | 说明 |
|------|------|------|------|
| ① | 释放源码 Payload | ~426 KB | 从 EXE 内置 Payload 释放 `app/` `config/` `pyproject.toml` `setup.py` 等，**无需 GitHub** |
| ② | 创建虚拟环境 | — | `.venv` 隔离 Python 依赖 |
| ③ | 安装依赖 | ~500 MB | 阿里云镜像 + 清华镜像（备用） |
| ④ | 模型准备 | — | 检查 Engine Pack → CRC32 校验安装 → 无本地包则在线下载四引擎模型 |
| ⑤ | 检查 FFmpeg | — | Lite 当前不内置 FFmpeg；需要系统 PATH 可用，或在 `bin/` 提供 `ffmpeg.exe`/`ffprobe.exe` |
| ⑥ | 生成 `.env` 配置 | — | 含合理默认值 |

> **断点续跑**：任何一步失败或中断，再次双击自动从断点继续。
> **源码固定**：本次发布源码来源固定为 Commit `4bdaa13`，不随 GitHub 上游变动。

4. 部署完成后打开 **Web 管理控制台**（默认 `http://127.0.0.1:8000`；未自动弹出时请手动访问）

> **Web 认证**：如需保护管理后台，在 `.env` 中设置 `ADMIN_PASSWORD=你的密码`。所有 API 操作将要求输入 Basic Auth（用户名固定为 `admin`）。

### 方式二：下载 Full 版（完整包，安装无需额外下载）

拿到 `BiliLiveCut-Portable-Full-*.zip`，解压到任意目录，双击 `BiliLiveCut-Portable.exe`。启动器自动检测同目录下的 `portable-python/`、`vendor/wheels/`、`bin/ffmpeg.exe`，安装过程无需从互联网拉取任何额外组件。

### 运行依赖锁维护

Portable 使用 Python 3.11 / 3.12 两套 Windows x64 完整依赖锁。锁文件覆盖直接依赖和全部传递依赖，每个条目都固定为 `==` 版本并校验所选 wheel 的 SHA-256。PyPI 没有提供 wheel 的五个纯 Python 包由受控脚本从固定 SHA-256 的源码构建，构建工具版本和时间戳同样固定。

```powershell
python -m pip install setuptools==83.0.0 wheel==0.46.3
python scripts/generate_portable_runtime_locks.py
```

Release CI 会对两套锁执行 `pip download --require-hashes`，并分别进行 Python 3.11 和 3.12 的全新虚拟环境 `--no-index` 离线安装、`pip check` 与核心模块导入测试。Full Launcher 会自动发现安装目录下的 `vendor/wheels` 并强制使用 `--no-index --require-hashes`，无需设置 `PIP_NO_INDEX`；若 Full wheelhouse 缺失或为空则直接失败，不会回退到在线镜像。不要通过删除哈希、添加 `--no-deps` 或跳过离线安装来规避锁文件错误。

### 方式三：开发者手动打包

在一台**能联网**的机器上执行：

```powershell
cd packaging\portable
pip install huggingface_hub
python build_bundle.py
```

打包完成后把整个 `packaging/portable/` 目录拷到目标机，双击 `launcher.exe` 即可以预置组件启动。

---

## 目录结构

```
packaging/portable/                     # ★ 即插即用分发版根目录 (原 Publish-PnP)
│
├── launcher.exe                     # ★★ 核心入口：双击即用，从内置 Payload 释放源码 + 部署
├── launcher.py                      # launcher.exe 的 Python 源码（可选，便于审查）
├── build_exe.py                     # Lite 版构建 (PyInstaller one-file)
├── build_full_bundle.py             # Full 完整包构建脚本
├── build_payload.py                 # Payload 构建器 (4bdaa13 → source_payload.zip)
├── build_bundle.py                  # 兼容旧版预置打包（保留）
├── portable_launcher.spec           # PyInstaller 规格文件
├── pip.ini                          # pip 镜像源配置（阿里云 + 清华备用）
├── .env.example                     # 配置模板（launcher.exe 自动生成 .env）
├── .gitignore                       # Git 忽略规则
├── tests/                           # 19 项 Portable 专项测试
├── build/                           # 构建临时文件（gitignore 忽略）
├── dist/                            # 构建产物
│   ├── payload/                     #   source_payload.zip + manifest
│   ├── lite/                        #   Lite EXE
│   ├── full/                        #   Full ZIP
│   └── engine-pack/                 #   Engine Pack ZIP
└── README.md                        # 本文件
```

> **源码去哪了？** `app/` `config/` `pyproject.toml` 等业务文件**不在分发目录中**，而是内嵌在 `launcher.exe` 内部作为 **source_payload.zip**（从当前发布基线 Commit `4bdaa13` 提取，SHA-256 可校验）。`launcher.exe` 首次运行时自动将 Payload 解压到 `runtime/releases/` 目录。PnP 目录始终保持最小体积，源码不受工作区未提交内容影响。

### 运行时动态生成（首次启动后）

```
├── runtime/                  # ★ Runtime 版本管理
│   ├── current.json          #   当前激活的 Release 信息
│   └── releases/
│       └── 0.1.15.1-alpha+4bdaa13+<payload-hash>/  # 内容寻址的固定版本源码
│
├── .venv/                    # Python 虚拟环境（launcher.exe 自动创建）
├── models/                   # 四引擎 ASR 模型 (由 Engine Pack 或在线下载安装)
│   ├── whisper/               #   Whisper large-v3-turbo (兜底引擎)
│   ├── paraformer/            #   Paraformer-zh (主引擎)
│   ├── sensevoice/            #   SenseVoice-Small (辅助特征)
│   └── funasr_nano/           #   Fun-ASR-Nano (低置信复核)
├── bin/                      # ffmpeg.exe / ffprobe.exe（首次运行下载，约 80 MB）
│
├── vendor/wheels/            # 预置依赖 wheel（build_bundle.py 构建时下载）
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
FFMPEG_PATH=ffmpeg    # Full 会使用随包 bin/ffmpeg.exe；Lite 需自行准备
FFPROBE_PATH=ffprobe  # Full 会使用随包 bin/ffprobe.exe；Lite 可使用系统 PATH
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
                            # 【账号管理】优先使用系统 Chrome；没有 Chrome 时自动下载 Chromium
                            # 手动填写格式：DedeUserID=xxx; SESSDATA=xxx; bili_jct=xxx
```

> **重要**：不填 Cookie 时弹幕采集不可用（B 站接口要求登录态），但录制本身不受影响。
> Portable 已包含 Playwright Python 组件。第一次使用【账号管理】时会优先启动电脑中已安装的 Google Chrome；找不到可用 Chrome 才联网下载 Playwright Chromium，并保存到 `vendor/playwright-browsers/` 供后续复用。下载期间请保持 Launcher 窗口、网络连接和足够磁盘空间。

### ASR 语音转写（本地多引擎，无需联网）

```ini
# 主引擎（V0.1.12）
ASR_PRIMARY=paraformer                  # paraformer=四层流水线（推荐）/ whisper=纯 Whisper 模式
ASR_PRIMARY_DEVICE=cpu                  # 主引擎设备，首次测试保持 cpu
ASR_AUXILIARY_DEVICE=cpu                # 辅助引擎设备
ASR_REVIEW_DEVICE=cpu                   # 复核引擎设备
ASR_FALLBACK_DEVICE=cpu                 # 兜底引擎设备
# 辅助层
ASR_SENSEVOICE=true                     # 情感/笑声/音乐/事件检测（需 funasr + modelscope）
ASR_FUNASR_REVIEW=true                  # 低置信片段复核（需 funasr + modelscope）
ASR_FALLBACK_WHISPER=true               # 主引擎失败时自动回退 Whisper
ASR_CONFIDENCE_THRESHOLD=-0.6           # 低于此置信度的片段触发复核
ASR_MODEL_REVISION=v2.0.4               # 模型版本锁定
WHISPER_MODEL=small                      # Whisper 兜底模型
WHISPER_DEVICE=cpu                       # Whisper 设备
WHISPER_COMPUTE_TYPE=int8                # CPU 推荐 int8
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

1. **双击发行包中的 Portable EXE** → 等待自动部署完成（Lite 版需联网安装依赖；Full 版依赖离线安装，但两者在没有 Engine Pack 时都需联网下载约 5.5 GB 模型）
2. 打开 Web 控制台；Cookie 为可选项，首次公开直播录制可跳过「账号管理」
3. **「直播间」Tab** → 粘贴直播间链接（如 `https://live.bilibili.com/123456`）→ 勾选授权确认 → 添加
4. 在房间卡片点击「开始录制」，并在「录制状态」确认片段数量增加
5. 首次测试先在 `storage/raw/session_<id>/` 确认原始片段；当前 Alpha 的房间级自动分析/渲染开关尚未完整暴露在 Web 页面
6. 已启用分析流水线并产生候选时，可在 **「候选审核」Tab** 点击「批准并出片」，再到 **「成品切片」Tab** 查看
7. 成品（MP4 + 封面 + 文案）输出在 `storage/clips/` 目录

> **可选增强**：在 `.env` 配置 `LLM_API_KEY` 后，高光复核和文案生成将由大模型辅助（否则走纯规则，同样可用）。
> **安全建议**：多人共用或暴露在局域网时，设置 `ADMIN_PASSWORD` 启用 Web 后台认证。

---

## 常用故障排查

| 问题 | 解决 |
|------|------|
| 启动报 `python` 不是命令 | Full 应检查 `portable-python/python.exe`；Lite 需安装 Python 3.11/3.12 并加入 PATH |
| Payload 释放失败 | 检查 EXE 完整性，SHA-256 不匹配时自动拒绝安装 |
| 下载模型卡住不动 | 关闭窗口重新双击，模型支持断点续传 |
| ASR 报 `funasr` / `modelscope` 未安装 | Full 应重新校验并解压完整 ZIP；Lite 需重新完成依赖安装 |
| ASR 主引擎无法加载 | 检查 `models/` 是否完整；无 Engine Pack 时首次需联网下载模型 |
| 弹幕采集提示 `code=-352` | 未配置 Bilibili Cookie；首次测试可设置 `COLLECT_DANMAKU=false` |
| LLM 调用报错 / 空结果 | 检查 `.env` 中 `LLM_API_KEY` 和 `LLM_BASE_URL` 是否正确 |
| FFmpeg 未找到 | Full 应检查 `bin/ffmpeg.exe` 与 `bin/ffprobe.exe` 是否完整；Lite 需自行准备 FFmpeg |
| API 请求返回 401 | `.env` 中设置了 `ADMIN_PASSWORD`，浏览器需要输入用户名 `admin` + 密码 |
| 修改 `.env` 后不生效 | 重启 `launcher.exe` |
| `launcher.exe` 被杀软拦截 | 添加信任白名单 |
| Runtime 损坏 | 删除 `runtime/` 目录后重新启动，Launcher 会自动重新安装 |

---

## 回主工程

此 `packaging/portable/` 目录是**发布给最终用户的即插即用版本**，源码固定于 `v0.1.15.1-alpha` 的发布基线 Commit。

- **主仓库**: `D:\Vibe\BiliLiveCut\README.md`
- **完整变更日志**: `D:\Vibe\BiliLiveCut\CHANGELOG.md`
- **Portable 构建文档**: 本文件及 `packaging/portable/` 下的 Python 模块
