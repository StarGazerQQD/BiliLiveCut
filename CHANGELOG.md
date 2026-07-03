# Changelog

## V0.1.5 Alpha (2026-07-03)

### 重构
- **去 Anthropic 化**:全网感资料库与 LLM 模块移除 "Anthropic/Claude" 硬编码文字,统一使用"大模型""LLM"等通用表述。
- **趋势采集独立 API 接入**:新增 `TREND_API_KEY` / `TREND_BASE_URL` / `TREND_MODEL` 配置项,语料采集可使用独立模型(如 DeepSeek V4),不再依赖通用 LLM 多模型列表。

### 变更
- `app/core/config.py`:新增 `trend_api_key`、`trend_base_url` 字段;废弃 `anthropic_model` 回退链。
- `app/analysis/llm.py`:新增 `call_trend_search()` 专用函数,趋势采集独立 API 优先,通用 LLM 兜底。
- `app/trends/collector.py`:改用 `call_trend_search()`。
- `.env.example`:移除 `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`,新增 `TREND_API_KEY`/`TREND_BASE_URL`。
- Dashboard HTML/JS、CLI 帮助文本、README 等 8+ 处 Anthropic 文案已统一修正。
- 版本号更新至 `V0.1.5 Alpha`。

## V0.1.4 Alpha (2026-07-03)

### 新增
- **GUI 账号登录**:Dashboard 新增「账号管理」Tab,点击登录弹出无痕浏览器窗口,用户扫码/密码登录后自动采集 Bilibili Cookie 并持久化存储,无需手动编辑 `.env`。
- **Cookie 统一管理**:新增 `app/core/cookie.py` 统一 Cookie 读取入口（运行时设置优先,`.env` 兜底）,所有模块（recorder/danmaku/service/cli）已统一接入。
- **Cookie 状态面板**:Dashboard 账号管理 Tab 实时展示当前登录态（UID、Cookie 摘要）,支持一键清除。

### 内部
- 新增 `app/web/login_handler.py`（Playwright 浏览器自动化登录流程）。
- 新增 `POST /api/login`、`GET /api/login/status`、`POST /api/login/clear`、`GET /api/cookie-status` 四个 API 端点。
- `launcher.exe` 重新编译。

## V0.1.3 Alpha (2026-07-02)

### 修复

**Bug 审计修复(审计范围:38 个源文件,共修复 26 个问题)**

- **CRITICAL**: OpenReviewAI 客户端改为模块级单例缓存,避免长时间录制耗尽连接池 (`llm.py:_get_client`)
- **CRITICAL**: `active_providers()` 增加 `base_url` 非空检查,防止空 URL 静默调用 OpenAI 官方 API (`llm_providers.py`)
- **CRITICAL**: 上传任务/裁剪偏移增加 `None` 检查,避免 `db.get()` 返回 `None` 时 `AttributeError` (`uploader.py`, `clipper.py`)
- **HIGH**: `danmaku_sentiment_score` 移除死代码(全表查询后丢弃),`_fetch_window_danmaku_texts`/`_danmaku_score` 改为 SQL 级时间过滤,消除全表扫描 O(n²) 性能退化 (`highlight.py`)
- **HIGH**: `Recorder._registered_paths` 改为内存缓存,每片段不再查全表 (`recorder.py`)
- **HIGH**: 弹幕写入改用 `add_all()` 批量插入,WebSocket 超时从 40s 降至 35s (`danmaku.py`)
- **MEDIUM**: `Recorder._seq` 在每次 `run()` 开始时复位,防止实例复用时序号不连续
- **MEDIUM**: `compute_recommended_threshold` 改为线性插值分位数,修复 P15 舍入误差 (`threshold_learning.py`)
- **MEDIUM**: `_fetch_window_danmaku_texts` 改为 `if content is not None` 不过滤空串弹幕 (`highlight.py`)
- **MEDIUM**: `_grab_cover` 调用包装 try/except,封面失败不影响切片产出 (`clipper.py`)
- **MEDIUM**: `dashboard_state` 改用 `COUNT(*)` 代替 `.all()` + `len()`,`pipeline_progress` 移除冗余字符串比较 (`service.py`)
- **LOW**: 数据库迁移异常改为 `logger.warning` 记录,权重默认值添加归一化说明 (`session.py`, `scoring_config.py`)

## V0.1.2 Alpha (2026-07-02)

### 新增

- **录制中断自动恢复**:Web 后台启动时自动扫描最近 24h 内中断的录制会话并恢复录制,
  有效应对进程崩溃/机器重启等场景
- **录制预约**:支持按时间计划自动启动录制(`blc schedule` CLI 命令),Dashboard 新增
  「录制预约」标签页,可创建/查看/删除预约;支持单次和每日重复
- **AI 阈值自学习**:用户审批/拒绝候选时自动记录评分与阈值快照,累计 10 条反馈后
  自动计算推荐阈值(P15 分位数),每房间独立学习,单次调整幅度上限 0.1
- **弹幕情绪分析**:基于弹幕文本的规则型情绪分析(重复率 + 感叹号密度 + 高情绪梗),
  作为高光评分的独立维度(`danmaku_sentiment`),完全离线,不依赖外部 API
- **流水线进度追踪**:Dashboard 录制状态页新增进度条,实时展示已录制/已转写/已评分
  片段数量与进度百分比
- **Dashboard 功能开关**:每个直播间卡片新增「预约录制」「阈值自学习」「弹幕情绪」
  三项开关,录制启动后自动锁定(不可更改,防止状态冲突)

### 变更

- 数据库模型新增 ``RecordingSchedule``、``ThresholdFeedback`` 两张表;``LiveRoom``
  表新增 ``schedule_enabled`` / ``auto_threshold_enabled`` / ``danmaku_sentiment_enabled`` 字段
- ``RecordingSession`` 表新增 ``last_reconnected_at`` 字段用于追踪重连成功时间
- 评分配置 ``scoring.yaml`` 增加 ``danmaku_sentiment`` 维度(权重 0.15)
- ``SessionStatus`` 新增 ``INTERRUPTED`` / ``RECONNECTED`` 状态
- 后端 ``init_db()`` 现已包含轻量迁移逻辑(为旧表补充缺失列)

### 修复

- **超管断流重连优化**:断流重连成功后首个片段写入即重置退避计数器(backoff→1),
  避免"稳定录制 30 分钟后再次被断流,却要白等 30s"。Dashboard 录制状态页
  现在展示最近重连成功时间与 ``RECONNECTED`` 绿色徽章。

## V0.1.1 Alpha (2026-07-02)

### 新增

- **`launcher.exe` 即插即用启动器**:用户拿到 `Public/` 目录后直接双击 `.exe` 即可运行,自动
  检测 Python 环境、创建虚拟环境、离线安装依赖、验证模型与 ffmpeg、启动 Web 管理后台并打开
  浏览器,不再依赖 `.ps1`/`.bat` 脚本,彻底规避系统安全策略拦截问题。
  - `Public/launcher.py` — 启动器源码
  - `Public/build_exe.py` — PyInstaller 一键编译脚本(`--onefile`)
  - `Public/launcher.exe` — 编译好的单文件可执行程序(约 8MB)

### 修复

- 修复 `Recorder.run()` 断流重连循环中 `backoff` 变量未初始化导致 `NameError` 的问题
  (`app/recording/recorder.py` 及 `Public/` 副本同步修复)

### 变更

- `.gitattributes` 规范化行尾(LF 入库 / 自动 CRLF Windows 检出),消除跨平台差异噪声
- `.gitignore` 显式添加 `!.env.example` 例外声明,确保配置模板(不含真实密钥)正常入库
- `Public/.gitignore` 排除 PyInstaller 构建临时文件(`build/`、`*.spec`)
- `Public/README.md` 更新文档,推荐 `launcher.exe` 为首选启动方式
- `Public/` 目录版本号与新主工程项目底代码同步至 `v0.1.1-alpha`

## V0.1.0 Alpha (2026-07-01)

首个可运行 Alpha 版本,涵盖 B 站 AI 直播实时切片全链路 MVP。

### 功能

- 直播源获取、FFmpeg 录制与分片、断流重连
- Whisper 本地转写、弹幕采集、网感资料库与定时采集
- 多维度高光评分、自动切片与后处理、LLM 文案生成
- OpenAI 兼容多模型 LLM 与失败回退
- Web 管理控制台(FastAPI)
- 上传队列与 Docker 部署
- 即插即用 `Public/` 分发包(Whisper 模型、ffmpeg、离线 wheel、一键 setup/check)

### 说明

- 版本号:PEP 440 `0.1.0-alpha`,展示名 **V0.1.0 Alpha**
- Alpha 阶段 API 与配置可能变动,生产使用前请自行评估
