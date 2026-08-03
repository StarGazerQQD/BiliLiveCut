# BiliLiveCut — AI 直播实时切片系统

[![CI](https://github.com/StarGazerQQD/BiliLiveCut/actions/workflows/ci.yml/badge.svg)](https://github.com/StarGazerQQD/BiliLiveCut/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/StarGazerQQD/BiliLiveCut?include_prereleases&sort=semver)](https://github.com/StarGazerQQD/BiliLiveCut/releases)
[![License](https://img.shields.io/github/license/StarGazerQQD/BiliLiveCut)](LICENSE)

**当前版本：V0.1.16.4 Alpha** (`0.1.16.4-alpha`)

面向 Bilibili 直播的全自动工作流：实时录制 → 转写 → 识别高光 → 生成切片 → 生成文案 → (可选)上传。
阶段 1–5 全链路已可用；即插即用分发包见 [`packaging/portable/`](packaging/portable/README.md)。普通 Windows 用户可直接阅读 [Portable 小白使用说明](packaging/portable/USER_GUIDE_ZH.md)。

> ⚠️ **合规声明**：本项目仅调用 Bilibili 网页播放器自身使用的公开接口，不做任何逆向、破解或绕过平台安全策略的行为。请**仅录制你拥有授权的内容**，遵守平台服务条款与合理访问频率。自动上传默认采用 `manual` 模式（只产出成品与元数据，不调用任何平台接口），零封号风险。

> ℹ️ **Engine Pack 说明**：GitHub Release 中**不含** ASR 模型引擎包（约 5.5 GB，超出上传限制）。用户需在本地自行生成：
> ```bash
> cd packaging/portable
> pip install modelscope huggingface_hub
> python download_engines.py          # 下载四引擎模型（约 5.5 GB）
> python build_engine_pack.py --from-cache  # 构建 Engine Pack ZIP
> ```
> 生成的 ZIP 放在便携版同目录下，首次启动时自动校验 CRC32/SHA-256 并安装模型。
> 正式构建会校验每个主模型、子模型和随附组件的固定 revision、目录契约及再分发许可证；包内附带 MIT、Apache-2.0 原文和[第三方模型声明](packaging/portable/licenses/THIRD_PARTY_NOTICES.md)。

> ⚖️ **许可证边界**：BiliLiveCut 项目代码采用 [MIT License](LICENSE)，Copyright (c) 2026 StarGazerQQD。随包第三方模型和组件继续适用各自的许可证与归属声明；项目的 MIT License 不改变任何第三方条款。

## V0.1.16 新特性：生产工作台与插件平台

**V0.1.16 把录制、审片、渲染与上传从单人同步操作升级为可恢复、可审计、可扩展的生产工作台。** 本次版本同时加入正式的本地插件接口和插件管理界面，并统一重做控制台的信息层级与交互样式。

### V0.1.16.4 Alpha：多直播间隔离与候选审片

- 默认录制分片目标改为约 5 分钟；FFmpeg 仍按实际关键帧完成切分，因此单片时长允许小幅浮动，也可通过 `SEGMENT_DURATION_S` 覆盖。
- 默认语音主引擎改为本地 Fun-ASR-Nano；无有效输出时依次回退 Paraformer 与 Whisper，保留实际引擎、失败原因及回退来源。
- 每个切片完成 ASR 后可调用已配置的 OpenAI 兼容大模型补全标点、整理可读正文并生成片段概括；控制台提供独立开关，失败时保留原始 ASR 且不中断分析。
- 实时转写页同时展示整理正文、片段概括、原始 ASR 和实际语音引擎，后续高光分析默认消费整理后的可读正文。
- 直播间录制选项及直播间独立功能开关增加未保存状态保护；存在本地草稿时暂停表单重绘，但开始、停止或恢复录制后仍会立即刷新运行状态、活动会话和操作按钮，保存草稿后恢复完整刷新。
- 多直播间同时监视时，直播间、会话、转写、弹幕、候选、审片队列和任务队列均显示“主播名 · 房间号”；任务链路同时校验分段、会话与候选的来源一致性。
- 修复弹幕基线把 SQLModel 标量时间误当元组解包、低分分析无法从 `analyzing` 直接结束、趋势 JSON 截断后整批丢弃，以及高光理由截断后已生成评分丢失的问题。
- 趋势采集单次最多请求 12 条，并只抢救截断前已完整闭合的 JSON 对象，避免猜测或写入半条数据。
- Portable 的 CAM++ v1.0.0 旧式 ModelScope 元数据会显式注册为 `CAMPPlus` 并校验本地权重；转写提交不再读取不存在的 `Settings.transcript_version`。
- 五分钟 TS 在识别前会转为 16 kHz 单声道 WAV，Fun-ASR-Nano 使用 FSMN-VAD 按默认不超过 30 秒拆句；重复退化会自动切换 Paraformer、Whisper，仍不合格的文本不会进入 LLM 或高光分析。
- 实时转写页支持“重新识别”：仅清理可安全重建的自动分析结果；人工审核、确认主题、渲染或发布数据均受保护，不会被覆盖。
- DeepSeek 思考模式只返回推理过程而没有最终正文时，会记录非敏感的结束原因与 token 统计，并关闭思考模式重试一次；推理过程不会写入转写或文案，空正文也不再被连通测试误报为成功。

### V0.1.16.2 Alpha：弹幕回退与生产开关

- 实时录制新增统一的流水线默认开关，Web 手动录制、预约、恢复和 CLI 会读取同一配置；CLI 仍可按单次录制显式覆盖。
- 弹幕链路按直播网页的 WBI 请求方式获取 token；登录访问遇到业务错误或鉴权拒绝时立即保持录制并切换匿名采集，同时按可配置间隔尝试恢复登录，达到单场失败上限后不再发送 Cookie。
- 高光评分插件增加 `off`、`shadow`、`champion` 三种运行模式、房间级覆盖和规则评分回退，并将稳定预测快照与人工审核结果回传插件形成反馈闭环。
- Portable Lite/Full 的 Python 3.11/3.12 严格哈希运行时锁已纳入 OpenAI 兼容 SDK，Launcher 会在启动体检中验证其可导入；大模型连通测试不再要求用户在 Portable 内手工执行源码安装命令。
- Portable 同时锁定并自动升级到 `pip 26.2`，并更新 Web、数据库、弹幕、ASR 与模型下载依赖；Full/Lite 均通过相同 SHA-256 锁和离线安装门禁验证。

### 审片边界与可追溯渲染

- 候选状态即可在审片工作台播放；尚无成品时会按需生成轻量预览，并与音频波形共用缓存。
- 默认保留爆点前 60 秒上下文（`config/scoring.yaml` 的 `context.pre_roll_s`），前文可跨原始分段拼接，转写和字幕也按候选时间范围合并。
- 高光复核理由、关键词等文本特征只读取音频峰值对应的候选窗口；审片正文与投稿文案再按最终保存的成片边界裁剪。存在词级时间戳时精确过滤，无时间戳的旧数据按时长比例近似裁剪，不会再把同一五分钟原始分段的后文当成当前候选内容。
- 审片边界调整改为严格 JSON 请求；服务端会校验起止顺序、最大时长以及边界是否落在真实录像覆盖范围内。
- 重渲染只使用已经保存的审片边界，不再临时改写候选记录；每次输出独立的版本文件，避免覆盖已有成品。
- 主剪辑、派生版本与合集 FFmpeg 统一使用可协作取消的子进程执行；取消时终止当前命令并清理未完成输出。

### 录制控制与直播打点

- 房间录制增加“停止并收尾”“强制停止”和“恢复录制”，并提供当前录制生命周期查询接口。
- 人工暂停状态持久化保存，进程重启或自动录制监控轮询时不会误拉起暂停中的房间。
- 支持直播过程中手动标记高光；会话结束后按真实媒体范围收敛标记窗口，避免生成越界候选。

### 多人审核与最小权限

- 新增独立审核队列和审核员角色/账号；审核员只能访问审核页面及其所需媒体，不能进入管理控制台的其他区域。
- 候选支持领取租约、防并发冲突、管理员强制接管、盲审、私有草稿、单步撤销和结构化审计记录。
- 审核界面和队列页面同步展示领取状态、边界草稿、处理进度与可执行操作，适配桌面和窄屏设备。

### 持久化后台作业与发布安全

- 新增持久化后台作业管理器，以及 `/api/jobs` 查询、取消和重试接口；候选出片、审片重渲染、合集渲染与上传均立即返回作业，不再阻塞 Web 请求。
- 后台作业支持幂等去重、进度、错误详情和重启恢复；控制台任务页可查看、取消或重试对应作业。
- 已经开始的远程上传禁止不安全取消；服务中断后进入结果核对状态而不自动重放，降低重复投稿风险。

### 插件接口、插件页与控制台重设计

- 新增 `app.plugins` 公共接口，提供类型化宿主上下文、生命周期钩子和命名空间设置存取；插件只在显式启用后导入和启动。
- 宿主从本地插件目录安全读取 `plugin.json` 清单，发现阶段不执行插件代码；无效清单、重复 ID、越界入口和符号链接会被隔离并报告。
- 控制台新增“插件”导航项：检测到插件后显示插件名称、启停开关和“设置”按钮；每个插件拥有由宿主渲染的独立设置页面。
- 根目录 [`plugin/`](plugin/README.md) 提供接口文档、Manifest JSON Schema 和可运行示例，并通过 `MANIFEST.in` 随源码发行包分发。
- 控制台采用分组侧边导航、统一深色视觉层级、焦点状态和响应式布局；录制、任务、审核与插件功能沿用同一套控件和状态反馈。
- 控制台新增“功能开关”页，按直播间集中展示五项流水线自动化开关、三个辅助开关和审核阈值，并提供上传、网感、插件及模型全局开关的直达入口。
- “模型”页可直接用尚未保存的当前表单执行连通测试；测试不会写入配置，并会保留各服务商的成功响应或错误详情。

### 版本与发布一致性

- Python 包、CLI、C/Cython、Rust、Portable、Docker、GitHub Actions、测试和用户文档统一升级为 `0.1.16.4-alpha`，Engine Pack 兼容区间同步为 `0.1.16.4-alpha ≤ app < 0.1.17`。
- 新功能覆盖单元、集成、前端语法和发布回归测试；CI 与 Release 门禁继续校验版本、固定源码、可复现 Payload、原生模块、依赖锁和制品完整性。

## V0.1.15 版本总结：Portable 发布链路完整收口

**V0.1.15 至 V0.1.15.3 将 V0.1.14 的架构与稳定性基础收敛为可复现、可离线安装、可供普通 Windows 用户直接测试的 Portable 发行链路。** 以下按补丁版本汇总这一版本线的全部改动。

### V0.1.15：源码基线与原生构建

- Portable 固定源码基线升级至当时的 `main` 提交 `4bdaa13`，移除已被新基线吸收的历史 Backport，并增加 Payload 业务源码逐文件一致性回归测试。
- PyO3/Rust 扩展构建显式使用当前虚拟环境的 Python，避免子进程找不到解释器后静默退回纯 Python 实现。

### V0.1.15.1：首次登录、依赖和入门文档

- 登录优先使用系统 Google Chrome；不可用时复用或按需安装 Playwright Chromium，并补齐状态提示和回归测试。
- Full/Lite 运行时锁加入 Playwright 与安装导入冒烟检查，离线 wheelhouse 最低数量同步更新为 110。
- 新增面向普通 Windows 用户的 [Portable 从零使用说明](packaging/portable/USER_GUIDE_ZH.md)，覆盖下载、哈希校验、解压、浏览器登录、首次录制和故障排查。
- 完善 Docker 构建上下文忽略规则，并修正文档中的启动、停止脚本名称。

### V0.1.15.2：Windows Portable 与离线运行修复

- Launcher、Engine Pack、Lite/Full、Payload 和旧版 Bundle 的 CLI 入口统一使用 UTF-8 stdout/stderr，并为不可编码字符保留回退表示，修复 Windows `cp1252` 控制台崩溃和误报。
- Windows Payload 改为在 Windows runner 构建，并以当前 Python ABI 的 `.pyd` 为成功条件；禁止混入 Linux `.so` 或旧 ABI，Full 离线冒烟会验证 C、Cython、Rust 三种原生后端均已加载。
- Cython 第二轮加速统一时间戳、长度和索引类型，修复 Unix epoch 分桶及长时间轴 SRT 与 Python fallback 不一致；Rust 构建改为实时输出 Cargo 日志。
- 字幕模板的 `line_gap_ms` 正式按词间停顿阈值断句；修复“配置已保存但不生效”。
- 系统 Chrome 与托管 Chromium 登录均启用 sandbox；Cookie 从独立 Playwright 上下文读取并按 Bilibili 域名边界筛选，修复新版 Chrome 登录完成后无法捕获 Cookie。
- 修复候选拒绝请求中的未闭合模板字符串，并增加全量前端 ES Module 静态语法回归检查。
- Full 安装后从内容寻址 Runtime 导入 `app.cli` 并保留失败输出；冻结 Launcher 改用绝对导入，PyInstaller 显式收集模型目录、版本加载器和 JSON 配置。
- Full 离线冒烟改用真实 Payload、Full venv 与 Fixture Engine Pack；Engine Pack 同步生成外部元数据，Full 清单加入流式 CRC32/哈希计算。
- Engine Pack 内部 Manifest 统一安装器 `format_version` 并使用真实加载器自检；正式清单只接受 `artifact_class=production`，隔离 CI Fixture 元数据。
- 补齐 `python -m app.cli` 入口，Launcher 显式调用 Typer `app.cli:app`，修复到达启动阶段后 Web 服务静默退出。

### V0.1.15.3：发布门禁、许可证和跨平台一致性

- Lite 首次安装 smoke 强制 UTF-8 输出；Doctor 冒烟在核对预期失败摘要后显式返回成功，避免 PowerShell 把预期非零状态误判为 Release 失败。
- Release 标签和 GitHub prerelease 判定统一按小写规范化，兼容已有 `-Alpha` 标签且不会误判为正式版。
- CI/Release 在构建 C/Cython 扩展前固定安装 `setuptools>=77` 与指定 Cython，修复 Windows Python 3.11 的 SPDX 构建兼容问题。
- Engine Pack 模型锁摘要统一按 LF 规范化换行计算，消除 Windows CRLF 与 Actions LF checkout 的跨平台 SHA-256 差异。
- 项目代码正式采用 MIT License，并将许可证纳入 Python 包、Payload、Portable Lite/Full、GitHub Release 与完整性门禁；Full 跨制品检查只匹配发行根目录直属许可证。
- sdist 明确收录前端 ES Module 交互检查脚本；Engine Pack 内嵌元数据必须与当前模型锁 SHA-256 一致。
- 修正 Portable Lite 构建命令示例，并增加防止文档重新引入过期命令的回归测试。

V0.1.15 最终形成 Lite 单 EXE、Full 离线包、内容寻址 Runtime、安全解压、可复现 Payload、Chrome 优先登录和跨制品哈希/许可证校验的完整闭环。它仍是 Alpha 版本，适合小规模、受控测试；发行细节见 [`packaging/portable/README.md`](packaging/portable/README.md)。

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
- **`bililivecut doctor`**: 15 项自检命令 (PASS/WARN/FAIL)，存在 FAIL 时返回非零退出码
- **CI 增强**: Portable Windows 运行时锁的阻断式 pip-audit + pytest-cov 覆盖率门禁，macOS 矩阵
- **290/290 测试通过**

## V0.1.12 新特性：多引擎 ASR 流水线

当前默认使用**四层本地 ASR 流水线**：

| 层级 | 引擎 | 功能 |
|------|------|------|
| **主引擎** | Fun-ASR-Nano | 中文语音识别与长音频转写 |
| **辅助特征** | SenseVoice-Small | 情感、笑声、音乐、事件检测 |
| **次级回退** | Paraformer-zh | FunASR 无有效输出时补充中文识别、标点与时间戳 |
| **最终兜底** | Whisper large-v3 / turbo | 前两级失败时自动回退 |

录制 TS 会先标准化为 16 kHz 单声道 PCM WAV；Fun-ASR-Nano 复用 Paraformer 随包的 FSMN-VAD，默认把单句限制在 30 秒（`ASR_VAD_MAX_SEGMENT_S`）。主引擎出现空输出或连续重复退化时自动回退；最终输出仍不合格时停止该任务，禁止污染文本进入 LLM 和高光分析。通过 `ASR_PRIMARY=paraformer` 或 `ASR_PRIMARY=whisper` 可切换主路径。全部模型懒加载，按 flags 独立启用/禁用。

## V0.1.11 新特性：数据一致性与流水线稳定性

- **五大独立开关**: `auto_record / auto_analyze / auto_render / auto_approve / auto_upload` 逐阶段独立判断，每次阶段转换重新读取房间配置
- **TaskWorker 真正并发**: 各阶段独立 `asyncio.create_task`，不串行阻塞；环境变量控制并发数
- **原子任务领取**: `UPDATE WHERE` 条件赋值，防多 Worker 抢同一任务
- **任务心跳 + stale 恢复**: 长任务周期性心跳更新，进程崩溃后自动恢复
- **数据模型约束**: 增加 UNIQUE 约束，防止双写不一致

## 功能进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 取流 + FFmpeg 录制 + 约 5 分钟关键帧对齐分片 + 入库 | ✅ 可用 |
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

# 6) 开始录制（Ctrl+C 停止；默认值由 RECORDING_PIPELINE_ENABLED 控制）
python -m app.cli record <db_id>
```

录制产物位于 `storage/raw/session_<id>/`。默认以 300 秒为分片目标；FFmpeg 按实际关键帧落盘，因此单片时长会在 5 分钟附近小幅浮动。

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

# 边录边分析（显式覆盖全局默认值）
python -m app.cli record <db_id> --pipeline
```

`RECORDING_PIPELINE_ENABLED=true` 时，Web 手动开始/恢复、预约、崩溃恢复以及未显式传参的 CLI 录制都会启用实时转写与高光分析。可在控制台“配置 → 功能开关 → 录制实时转写”覆盖该默认值；CLI 可用 `--pipeline` 或 `--no-pipeline` 对单次录制覆盖。`TRANSCRIPT_LLM_REFINE_ENABLED=true` 时，每个切片完成本地 ASR 后还会调用已配置的大模型补全标点、整理正文并生成片段概括；调用失败时保留原始 ASR，不阻断流水线。这两个开关都从下一次开始或恢复录制生效。

主播下播、断流或平台暂时无法返回播放地址时，录制器会继续重试，但不会无限挂起。连续失败达到 `RECORDING_RECONNECT_MAX_ATTEMPTS`（默认 20 次）或从断流开始经过 `RECORDING_RECONNECT_MAX_ELAPSED_S`（默认 300 秒）时，任一条件先满足都会自动结束本场录制并正常执行会话收尾。成功恢复并产出新片段后，次数和计时都会归零。将某一项设为 `0` 可单独禁用该限制；两项都设为 `0` 会恢复无限重试，不建议用于无人值守录制。

`COLLECT_DANMAKU=true` 时，录制器会按 Bilibili 直播网页的 WBI 请求格式获取短期弹幕 token。有已保存 Cookie 时优先使用登录请求，并以 Cookie 中的 `DedeUserID` 完成 WebSocket 鉴权；登录接口返回业务错误或登录鉴权被拒绝后，会立即改用不带 Cookie、`uid=0` 的匿名链路，录制和弹幕接收不会因此停止。匿名连接存活期间，程序按 `DANMAKU_LOGIN_RETRY_INTERVAL_S` 定时探测登录链路；单场累计失败达到 `DANMAKU_LOGIN_RETRY_MAX_ATTEMPTS`（默认 5 次，首次计入）后，本场不再发送 Cookie。将最大次数设为 `0` 可始终匿名采集。

默认启用以 Fun-ASR-Nano 为首选的四层 ASR 流水线（`ASR_PRIMARY=funasr_nano`），也可切换到 Paraformer 或纯 Whisper：

```env
ASR_PRIMARY=funasr_nano       # 默认；也可设为 paraformer 或 whisper
ASR_VAD_MAX_SEGMENT_S=30      # Nano 的 FSMN-VAD 单句上限（秒）
ASR_FALLBACK_WHISPER=true     # 主引擎失败时自动兜底
TRANSCRIPT_LLM_REFINE_ENABLED=true  # 用已配置 LLM 整理正文并生成片段概括
TRANSCRIPT_LLM_REFINE_MAX_TOKENS=65536  # 五分钟转写整理的最大输出预算
HIGHLIGHT_LLM_MAX_TOKENS=65536      # 高光复核的最大输出预算（含推理 token）
```

转写整理和高光复核默认各预留 `65536` 个最大输出 token，避免推理模型在处理五分钟切片时耗尽额度而没有正文；可按模型能力分别在 `128-65536` 和 `512-65536` 范围内调低。该值是单次请求上限，实际用量仍以模型返回的 token 数为准。

实时转写页的“重新识别”可修复历史污染文本。操作会删除当前转写和未人工处理、未渲染的自动候选后重新排队；若已有人工审核、确认主题、成片或正在运行的任务，服务端会拒绝覆盖并说明原因。

**工作原理与成本控制**：先用零成本规则特征（音量峰值、关键词、语速突增、音频特征、弹幕热度）算出 `rule_score`；只有超过初筛阈值才调用大模型复核。新直播间默认初筛/候选/人工审核阈值为 `0.35/0.45/0.40`，自动批准/发布阈值为 `0.72/0.80`，也可通过 `.env` 的 `HIGHLIGHT_*_THRESHOLD` 与 `AUTO_PUBLISH_THRESHOLD` 调整；已存在直播间保留其显式保存值，可在“功能开关”中调整。未配置 `LLM_API_KEY` 时自动走**纯规则模式**，完全可用、零费用。

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

功能概览：**直播间管理 / 录制状态 / 实时转写 / 候选审核（横屏审片工作台）/ 成品切片 / 主题管理 / 合集编辑 / 插件中心 / 运维面板 / 任务队列监控 / 上传设置**。

插件默认从 `./storage/plugins` 读取，可通过 `PLUGIN_DIR` 修改。扫描只读取 `plugin.json`，入口代码仅在管理员显式启用后执行；插件与主程序同进程运行，因此只应启用可信插件。开发接口、清单 Schema 和最小示例见 [`plugin/README.md`](plugin/README.md)。

### 可插拔高光评分

插件 API v1 支持 `highlight_scorer` 能力，同一时间只允许启用一个提供者。宿主把片段、会话、房间、转写、词时间戳、弹幕窗口、聚合音频、ASR 辅助信息和规则分转换为无 ORM 的只读 DTO；插件不能直接依赖主程序数据库。

- `off`：不执行模型，保持原规则链路；
- `shadow`：保存 Champion/Shadow 概率和模型身份，不改变最终主评分；
- `champion`：用 Champion 概率替换规则主评分，再进入原有 LLM 融合、阈值、去重和审核流程；
- 插件缺失、停用、模型不可用、Schema 不兼容、异常或概率非法时，宿主记录原因并回退规则评分。

人工审核提交后，宿主把明确批准映射为正样本，把 `rejected/not_exciting` 映射为负样本；保留、上下文/边界/字幕/画面问题及撤销不被伪造为负样本，而是通知原插件删除同一 `sample_id` 的旧标签。反馈写入失败不会回滚已经提交的审核。房间级模式可在控制台“配置 → 功能开关 → 高光评分插件”中设为继承、关闭、Shadow 或 Champion。

真实插件联调是跨仓库显式检查，不进入宿主默认测试集，也不会在缺少外部插件时产生跳过项。Windows PowerShell 可执行：

```powershell
$env:BILILIVECUT_HIGHLIGHT_SOURCE = "D:\path\to\BiliLiveCut_HighLight"
python -m pytest scripts/external_tests/test_highlight_plugin_external.py
```

独立参考实现、训练 CLI 和模型注册表位于 [StarGazerQQD/BiliLiveCut_Highlight](https://github.com/StarGazerQQD/BiliLiveCut_Highlight)。

多人审核入口为 `/review/queue`。管理员仍使用 `ADMIN_PASSWORD`；审核员账号、领取租约和盲审开关在 `.env` 中配置：

```env
ADMIN_PASSWORD=change-admin-password
REVIEWER_ACCOUNTS_JSON={"reviewer":"change-reviewer-password"}
REVIEW_CLAIM_TTL_S=900
REVIEW_BLIND_MODE=true
```

审核员只能访问 `/review/*` 和审片所需的视频/封面接口；管理员可显式强制接管他人的有效领取。远程部署必须同时设置 `ADMIN_PASSWORD`，真实密码不得提交到仓库。

批准出片、审核重渲染、合集渲染和上传/重试会立即返回后台作业，不再长时间占用 HTTP 请求。作业状态持久化，可在控制台“任务队列”查看进度、错误和结果，也可通过 `GET /api/jobs`、`GET /api/jobs/{job_id}` 查询，通过 `POST /api/jobs/{job_id}/cancel` 或 `/retry` 取消、重试。服务重启会恢复安全可重跑的渲染作业；已开始的上传不会自动重放，避免平台已收件但本地未知时重复投稿。FFmpeg 渲染取消会主动终止当前外部进程并清理未完成输出。

### 自动化开关（V0.1.11）

五个独立开关，可自由组合：

- `auto_record` — 自动检测开播并录制
- `auto_analyze` — 自动转写 + 高光分析
- `auto_render` — 自动生成切片
- `auto_approve` — 高分候选自动批准
- `auto_upload` — 自动提交上传

每个开关逐阶段独立判断，修改后未完成任务按新配置执行。支持房间级别配置覆盖。

Portable Web 控制台可在“配置 → 功能开关”中按直播间独立修改上述五项开关；预约录制、阈值自学习、弹幕情绪与审核阈值也集中在同一页。房间级 `auto_upload` 仍需配合“上传与发布”页的全局上传总开关。

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

## 测试与发布验证

```powershell
pip install -e ".[dev,web]" `
  --index-url https://mirrors.aliyun.com/pypi/simple/ `
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple/

# 常规测试（主线 + Portable）
pytest -q

# 前端 ES Module、初始刷新、事件绑定与标签切换（需 Node.js）
node scripts/check_frontend_interactions.mjs

# 提交前完整 CI 门禁（含覆盖率）
python scripts/ci_gate.py

# 发布前严格门禁（含联网依赖审计和 Payload 构建）
python scripts/release_gate.py
```

`release_gate.py` 是 fail-closed 的：依赖审计无有效 JSON、出现未豁免漏洞、测试 skip 或构建不完整都会返回非零退出码。

## 排错

| 现象 | 排查 |
|---|---|
| `ffmpeg 不是内部或外部命令` | 安装 FFmpeg 或在 `.env` 设置 `FFMPEG_PATH` |
| `check` 显示未开播 | 主播未直播时无流，属正常 |
| 取流报错 / 403 | 部分高清晰度需登录态，可在 `.env` 配置 `BILIBILI_COOKIE` |
| 弹幕 token 返回 `code=-352` | 登录请求失败时会立即匿名兜底并定时重试；匿名请求也被风控时会按配置间隔重试，录制与实时转写继续。无需反复手动登录 |
| 片段未生成 | 看 `storage/logs/blc.log` 中 `[ffmpeg]` 行 |
| ASR 主引擎未加载 | 确认 `pip install funasr modelscope` 已执行 |

## 目录结构

```
├── app/                     # 后端主包 (sources / recording / analysis / clipping / publishing / pipeline / web)
├── config/                  # 权重与关键词 YAML
├── tests/                   # 主线单元、集成与故障注入测试
├── storage/                 # 运行产物 (.gitignore)
├── packaging/portable/      # 即插即用分发版 (原 Publish-PnP)
├── pyproject.toml           # 项目配置
├── .env.example             # 配置模板
└── README.md                # 本文件
```

## 许可证

BiliLiveCut 项目代码采用 [MIT License](LICENSE)，Copyright (c) 2026 StarGazerQQD。第三方模型、运行组件及其许可证材料独立列于 [第三方模型声明](packaging/portable/licenses/THIRD_PARTY_NOTICES.md)，不因项目采用 MIT License 而改变。
