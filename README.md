# BiliLiveCut — AI 直播实时切片系统

[![CI](https://github.com/StarGazerQQD/BiliLiveCut/actions/workflows/ci.yml/badge.svg)](https://github.com/StarGazerQQD/BiliLiveCut/actions/workflows/ci.yml)

**当前版本：V0.1.14.9 Alpha** (`0.1.14.9-alpha`)

面向 Bilibili 直播的全自动工作流：实时录制 → 转写 → 识别高光 → 生成切片 → 生成文案 → (可选)上传。
阶段 1–5 全链路已可用；即插即用分发包见 [`packaging/portable/`](packaging/portable/README.md)。

> ⚠️ **合规声明**：本项目仅调用 Bilibili 网页播放器自身使用的公开接口，不做任何逆向、破解或绕过平台安全策略的行为。请**仅录制你拥有授权的内容**，遵守平台服务条款与合理访问频率。自动上传默认采用 `manual` 模式（只产出成品与元数据，不调用任何平台接口），零封号风险。

> ℹ️ **Engine Pack 说明**：GitHub Release 中**不含** ASR 模型引擎包（约 5.5 GB，超出上传限制）。用户需在本地自行生成：
> ```bash
> cd packaging/portable
> pip install modelscope huggingface_hub
> python download_engines.py          # 下载四引擎模型（约 5.5 GB）
> python build_engine_pack.py --from-cache  # 构建 Engine Pack ZIP
> ```
> 生成的 ZIP 放在便携版同目录下，首次启动时自动校验 CRC32/SHA-256 并安装模型。

## V0.1.14.7 新特性：发行结构重构

解决中国大陆 GitHub 不稳定问题，建立从固定 Git Commit 提取源码、嵌入 Portable EXE 的发行链路。彻底摆脱首发时对 GitHub 的依赖。

```text
用户取得 Portable EXE → 双击运行 → 读取内置 Payload → 不访问 GitHub → 校验 SHA-256 → 释放源码 → 启动
```

| 特性 | 说明 |
|------|------|
| **Source 固定** | 源码始终来自 `731a31c`，通过 `git archive` 提取，不混入工作区和后续改动 |
| **零 GitHub 请求** | 首次启动完全从 EXE 内置 Payload 释放源码，不访问 GitHub |
| **可复现 Payload** | 相同输入构建两次 SHA-256 完全一致 (`93ff7bfa...`) |
| **原子 Runtime 安装** | `staging → rename` 原子切换，`current.json` 原子更新 |
| **Lite / Full 双发行** | Lite: 轻量化单 EXE，安装时联网下载依赖；Full: 预置 Portable Python + Wheels + FFmpeg，安装无需额外下载 |
| **Zip Slip 防护** | 解压拒绝绝对路径、`..` 和盘符路径 |

测试: 19 项 Portable 测试 + 308 项主项目测试全部通过，Ruff 零错误。

详见 [`packaging/portable/README.md`](packaging/portable/README.md)。

## V0.1.14 新特性：架构重构 + 稳定性收口

### 模块拆分与可维护性重构

- **仓库清理**: 删除临时 CI 日志、归档 CHANGELOG、测试目录分层 (`unit/` / `integration/` / `fault_injection/`)
- **加速模块归拢**: C/Cython/Rust/Python fallback 统一归入 `app/accelerators/`
- **深层拆分**: `task_worker.py` (1667行) 拆分为 4 阶段 compute/commit + 独立 Worker 模块；CLI 拆分子命令；Web 拆分子路由和服务；DB 拆分子模型；前端 JS 模块化
- **版本化 Schema**: 轻量 `schema_meta` 元信息表 + SHA-256 指纹，不兼容数据库拒绝启动

### 全链路崩溃安全 (Stability Closure)

- **Durable Journal**: DB 不可用时远程上传成功结果写入 JSONL 持久化，重启后回填
- **异常分类**: `classify_upload_error` 精确区分可重试/不确定/永久失败，禁止重复投稿
- **Stale Recovery**: 超时 `IN_PROGRESS` Attempt → `RECONCILIATION_REQUIRED`，`full_recovery()` 全量恢复统一入口
- **308/308 测试全部通过**，Ruff 零错误

## V0.1.13 新特性：运行时集成与 Golden Path

### 核心架构升级

- **TaskLease + Compute/Commit 分离**: 4 阶段全部拆分为纯计算 (compute) + 原子提交 (commit)，租约贯穿全链路
- **ResourceBudget 资源预算**: CPU/GPU/内存/显存四维资源池，任务领取前 reserve，不足时拒绝
- **两级磁盘保护**: `LOW_DISK_THRESHOLD_GB`(20GB) / `CRITICAL_DISK_THRESHOLD_GB`(5GB)，危险磁盘安全停止录制
- **FFmpeg 错误分类**: 结构化异常类型，永久错误不无限重试
- **Bilibili 风控熔断**: `CircuitBreaker` 房间级熔断，403/412 触发后退避
- **弹幕分级采样**: SC/互动 100% 采集，普通 30%，高密度降至 10%

### 安全与运维

- **Web loopback guard**: 非本机监听 + 空密码 → 拒绝启动，认证用 `secrets.compare_digest`
- **敏感信息脱敏**: Cookie/SESSDATA/API Key/Token 统一脱敏器
- **`bililivecut doctor`**: 15 项自检命令 (PASS/WARN/FAIL)
- **CI 增强**: pip-audit + pytest-cov 覆盖率门禁，macOS 矩阵
- **290/290 测试通过**

## V0.1.12 新特性：多引擎 ASR 流水线

默认引擎从 Whisper 单引擎升级为**四层流水线**：

| 层级 | 引擎 | 功能 |
|------|------|------|
| **主引擎** | Paraformer-zh | 中文文本、词级时间戳、标点 |
| **辅助特征** | SenseVoice-Small | 情感、笑声、音乐、事件检测 |
| **低置信复核** | Fun-ASR-Nano | 低分 / 非中文片段复核 |
| **最终兜底** | Whisper large-v3 / turbo | 保留切换，主引擎失败时自动回退 |

通过 `ASR_PRIMARY=whisper` 可随时切回纯 Whisper 模式。全部模型懒加载，按 flags 独立启用/禁用。

## V0.1.11 新特性：数据一致性与流水线稳定性

- **五大独立开关**: `auto_record / auto_analyze / auto_render / auto_approve / auto_upload` 逐阶段独立判断，每次阶段转换重新读取房间配置
- **TaskWorker 真正并发**: 各阶段独立 `asyncio.create_task`，不串行阻塞；环境变量控制并发数
- **原子任务领取**: `UPDATE WHERE` 条件赋值，防多 Worker 抢同一任务
- **任务心跳 + stale 恢复**: 长任务周期性心跳更新，进程崩溃后自动恢复
- **数据模型约束**: 增加 UNIQUE 约束，防止双写不一致

## 功能进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 取流 + FFmpeg 录制 + 60s 分片 + 入库 | ✅ 可用 |
| 2 | 多引擎 ASR / 规则+LLM 高光判断 | ✅ 可用 |
| 3 | 自动切片 + 后处理 + 文案 | ✅ 可用 |
| 4 | Web 管理后台 | ✅ 可用 |
| 5 | 上传队列 + Docker 部署 | ✅ 可用 |

## 环境要求

- Python **3.11 / 3.12**（推荐；部分 AI 依赖对 3.13/3.14 的预编译包可能尚未就绪）
- FFmpeg（已加入 PATH，或在 `.env` 指定 `FFMPEG_PATH`）
- *(可选)* C 编译器（MSVC/MinGW/GCC）— 用于编译加速模块；如不可用，自动回退纯 Python 实现

### C / Rust / Cython 加速模块

自 V0.1.9 起，高频 CPU 热点使用多语言加速，优先级：Rust → Cython → C → 纯 Python。

- **Aho-Corasick 多模式匹配** 20–50×（C）
- **余弦相似度 / 字符 bigram** 3–8×（C）
- **聚类矩阵 O(N²)** 5–15× 纯 Python / **30–80× Rust+rayon** 并行
- **弹幕基线分桶 + 中位数** 10–30×（Cython）
- **SRT 字幕组装** 3–8×（Cython）

```powershell
# 自动检测：pip install -e . 自动尝试编译；失败 → 自动回退 Python 实现
# Rust 编译（可选，需安装 Rust 工具链）：
python tools/native/build_rust.py
# C 扩展手动编译（Windows 需 Visual Studio Build Tools）：
python setup_c.py build_ext --inplace
```

### Python 依赖源

境内安装推荐优先使用**阿里云 PyPI 镜像**，清华大学镜像作为备用源：

```
默认源  https://mirrors.aliyun.com/pypi/simple/
备用源  https://pypi.tuna.tsinghua.edu.cn/simple/
```

可通过环境变量覆盖（不修改系统级 pip 配置）：

```
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
PIP_EXTRA_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
```

## 快速开始（Windows PowerShell）

```powershell
cd D:\Vibe\BiliLiveCut

# 1) 创建虚拟环境并安装
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e . `
  --index-url https://mirrors.aliyun.com/pypi/simple/ `
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/

# 2) 准备配置
Copy-Item .env.example .env   # 按需修改

# 3) 初始化数据库
python -m app.cli init

# 4) 登记一个你有授权的直播间
python -m app.cli add-room "https://live.bilibili.com/你的房间号" --authorize

# 5) 查看 / 检查
python -m app.cli list-rooms
python -m app.cli check 你的房间号

# 6) 开始录制（Ctrl+C 停止）
python -m app.cli record <db_id>
```

录制产物位于 `storage/raw/session_<id>/`，每 60 秒一个 `.ts` 片段。

## 阶段 2：多引擎 ASR 转写 + 高光判断

### ASR 流水线（V0.1.12）

安装 AI 依赖：

```powershell
pip install -e ".[asr]" `
  --index-url https://mirrors.aliyun.com/pypi/simple/ `
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/
pip install -e ".[llm]" `
  --index-url https://mirrors.aliyun.com/pypi/simple/ `
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/
# V0.1.12: 多引擎 ASR 需要 funasr + modelscope
pip install funasr modelscope
```

```powershell
# 对已录制的片段
python -m app.cli process <segment_id>
python -m app.cli list-candidates       # 查看高光候选

# 边录边分析
python -m app.cli record <db_id> --pipeline
```

默认启用四层 ASR 流水线（`ASR_PRIMARY=paraformer`），也可切回纯 Whisper：

```env
ASR_PRIMARY=whisper           # 回退纯 Whisper 模式
ASR_FALLBACK_WHISPER=true     # 主引擎失败时自动兜底
```

**工作原理与成本控制**：先用零成本规则特征（音量峰值、关键词、语速突增、音频特征、弹幕热度）算出 `rule_score`；只有超过初筛阈值才调用大模型复核。未配置 `LLM_API_KEY` 时自动走**纯规则模式**，完全可用、零费用。

> **大模型选型（境内）**：系统采用 **OpenAI 兼容协议**，可对接 DeepSeek / 通义千问 / Kimi / 智谱 GLM——只需配 `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`。

## 阶段 3：自动切片 + 后处理 + 文案

把高光候选生成为可投稿的 MP4：

```powershell
python -m app.cli produce <candidate_id>     # 切片 + 文案一步到位

# 全自动链路
python -m app.cli record <db_id> --pipeline --produce
```

**后处理选项**（在 `.env` 配置）：响度标准化 `CLIP_LOUDNORM`、去首尾静默 `CLIP_REMOVE_SILENCE`、烧录字幕 `CLIP_SUBTITLE`、最大时长 `CLIP_MAX_DURATION_S`、画质 `CLIP_VIDEO_CRF`。

**多版本出片**：每个 HighlightEvent 可生成多个 ClipVariant（单段版、完整上下文版、带字幕版、无字幕净版、投稿压制版、高码率归档版），横屏输出以 1920×1080 为主。

## 阶段 4：Web 管理后台

```powershell
pip install -e ".[web]" `
  --index-url https://mirrors.aliyun.com/pypi/simple/ `
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/
python -m app.cli serve              # 默认 http://127.0.0.1:8000
```

功能概览：**直播间管理 / 录制状态 / 实时转写 / 候选审核（横屏审片工作台）/ 成品切片 / 主题管理 / 合集编辑 / 运维面板 / 任务队列监控 / 上传设置**。

### 自动化开关（V0.1.11）

五个独立开关，可自由组合：

- `auto_record` — 自动检测开播并录制
- `auto_analyze` — 自动转写 + 高光分析
- `auto_render` — 自动生成切片
- `auto_approve` — 高分候选自动批准
- `auto_upload` — 自动提交上传

每个开关逐阶段独立判断，修改后未完成任务按新配置执行。支持房间级别配置覆盖。

## 阶段 5：上传队列 + 部署

- 默认 `ManualUploader`：不调用任何平台接口，只导出待上传清单，**零封号风险**。
- `BiliupUploader`：默认关闭，需手动在 Web 后台开启并配置 `BILIUP_UPLOAD_CMD`。⚠ 走你自己的登录态，风险自负。

**上传前置校验**：文件完整性、标题/简介合规查重、投稿频率限制（`UPLOAD_MAX_PER_HOUR`），失败重试（`UPLOAD_MAX_RETRIES`）。

### Docker 部署

```bash
cp .env.example .env
# Docker 构建上下文为仓库根目录，Compose 文件位于 packaging/docker/
docker compose -f packaging/docker/compose.yaml up --build -d
# 打开 http://localhost:8000
```

或者使用便捷脚本：

```bash
# Windows
scripts\docker-up.bat

# Linux/macOS
bash scripts/docker-up.sh
```

详情参见 [packaging/docker/README.md](packaging/docker/README.md)。

## 测试

```powershell
pip install -e ".[dev]" `
  --index-url https://mirrors.aliyun.com/pypi/simple/ `
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/
pytest -q
```

## 排错

| 现象 | 排查 |
|---|---|
| `ffmpeg 不是内部或外部命令` | 安装 FFmpeg 或在 `.env` 设置 `FFMPEG_PATH` |
| `check` 显示未开播 | 主播未直播时无流，属正常 |
| 取流报错 / 403 | 部分高清晰度需登录态，可在 `.env` 配置 `BILIBILI_COOKIE` |
| 片段未生成 | 看 `storage/logs/blc.log` 中 `[ffmpeg]` 行 |
| ASR 主引擎未加载 | 确认 `pip install funasr modelscope` 已执行 |

## 目录结构

```
├── app/                     # 后端主包 (sources / recording / analysis / clipping / publishing / pipeline / web)
├── config/                  # 权重与关键词 YAML
├── tests/                   # 测试 (308 项)
├── storage/                 # 运行产物 (.gitignore)
├── packaging/portable/      # 即插即用分发版 (原 Publish-PnP)
├── pyproject.toml           # 项目配置
├── .env.example             # 配置模板
└── README.md                # 本文件
```
