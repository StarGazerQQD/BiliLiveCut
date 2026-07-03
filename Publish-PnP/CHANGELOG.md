# Changelog — 即插即用版 (Publish-PnP)

## V0.1.8 Alpha (2026-07-04)

- P0 管线强化:hotword注入、aliases纠错、批量审核、ASS模板管理
- 详见根目录 `CHANGELOG.md`。

---

## V0.1.7.2 Alpha (2026-07-04)

- 半成品清理:状态机迁移、章节持久化、asr_text填充、ClipVariant落地
- 文档清理:删除YouTube引用、pip源顺序统一
- 详见根目录 `CHANGELOG.md`。

---

## V0.1.7.1 Alpha (2026-07-04)

- 安全修复:FFmpeg 注入/路径遍历/XSS/代码质量
- P3 文件同步 + 版本对齐
- 详见根目录 `CHANGELOG.md`。

---

## V0.1.7 Alpha (2026-07-03)

- P1 补齐:音频波形 + 字幕时间轴
- P2:合集编辑/渲染/文案 + 房间级热词配置
- 详见根目录 `CHANGELOG.md`。

---

## V0.1.6 Alpha (2026-07-03)

### P0
- 弹幕评分重构、自动化开关拆分、持久化任务队列、pip 镜像阿里云优先。详见根目录 `CHANGELOG.md`。

### P1
- 横屏审片工作台、主题识别与聚类、HighlightEvent/ClipVariant 模型。详见根目录 `CHANGELOG.md`。

- `launcher.exe` 需重新编译同步。

## V0.1.5.1 Alpha (2026-07-03)

### 修复
- Dashboard 上传开关不再被 5 秒轮询自动取消勾选。

## V0.1.5 Alpha (2026-07-03)

### 重构
- 去 Anthropic 化、趋势采集独立 API 接入点。详见根目录 `CHANGELOG.md`。
- `launcher.exe` 重新编译同步。

## V0.1.4 Alpha (2026-07-03)

### 新增
- **GUI 账号登录**:Dashboard「账号管理」Tab,浏览器扫码登录自动采集 Cookie。详见根目录 `CHANGELOG.md`。

### 内部
- `launcher.exe` 重新编译同步。

## V0.1.3 Alpha (2026-07-02)

同步主工程 v0.1.3-alpha 全部 Bug 修复(审计共修复 26 个问题)。

### 修复

- 同步主工程全部 26 项代码质量修复
  - **CRITICAL**: OpenAI 客户端连接池泄漏、`base_url` 空值检查、`None` 崩溃保护
  - **HIGH**: 弹幕查询全表扫描→SQL 过滤、`_registered_paths` 内存缓存、`add_all` 批量写入
  - **MEDIUM**: 分位数精度、WebSocket 超时、封面异常保护、`COUNT(*)` 计数优化
  - **LOW**: 迁移异常日志记录、权重归一化说明

### 变更

- 版本号同步 `v0.1.2.2-alpha` → `v0.1.3-alpha` (与主工程对齐)
- ``launcher.exe`` 重新编译(8.2 MB)
- **移除冗余脚本**:``setup.bat/ps1``、``install.bat/ps1``、``run.bat/ps1``、``check.bat/ps1``
  — 全功能已被 ``launcher.exe`` 覆盖,无需保留
- ``README.md`` 精简:移除脚本方式章节,目录结构和体检说明更简洁

## V0.1.2.2 Alpha (2026-07-02)

### 新增

- **``launcher.exe`` 全自展开模式**:一个 exe 从零搭建完整运行环境,无需拷贝任何其他文件
  - 自动从 GitHub 下载源码（app/config/pyproject.toml/requirements-bundle.txt）
  - 联网安装依赖（清华+阿里云镜像,无需预打包 vendor/wheels）
  - 自动下载 Whisper large-v3-turbo 模型（hf-mirror.com,~1.6GB）
  - 自动下载 ffmpeg/ffprobe（BtbN 静态构建,~80MB）
  - 自动生成 .env 配置文件
  - 所有步骤断点续跑:组件就位则跳过,中断后再次运行接续
- **启动前目录扫描**:显示工作目录文件总数,逐项报告 6 大组件状态
  （[OK]  已就绪 / [---]  需下载）,一目了然

### 变更

- ``launcher.py`` 完全重写（~500 行 → ~690 行）:集成 GitHub 下载、在线 pip、模型/ffmpeg 下载、目录扫描全流程
- ``launcher.exe`` 重新编译（8.2 MB）
- 版本号同步:`v0.1.2.1-alpha` → `v0.1.2.2-alpha`(展示名 `V0.1.2.2 Alpha`)
  - `pyproject.toml`、`README.md`、`launcher.py` 统一更新

## V0.1.2.1 Alpha (2026-07-02)

### 新增

- **源码随仓库分发**:``app/``、``config/``、``pyproject.toml`` 现已入库,
  ``git clone`` 即可用,无需先跑 ``build_bundle.py``

### 变更

- ``.gitignore`` 移除 ``app/``、``config/``、``pyproject.toml``、``README_MAIN.md`` 的排除,改为入库
- ``README.md`` 重构:两种方式（A:双击 .exe / B:build_bundle 离线包）
- 版本号同步:`v0.1.2-alpha` → `v0.1.2.1-alpha`(展示名 `V0.1.2.1 Alpha`)
  - `pyproject.toml`、`README.md`、`launcher.py` 统一更新

## V0.1.2 Alpha (2026-07-02)

同步主工程 v0.1.2-alpha 全部功能。

### 新增

- **录制中断自动恢复**:Web 后台启动时自动扫描最近 24h 内中断的录制会话并恢复录制
- **录制预约**:支持按时间计划自动启动录制,Dashboard 新增「录制预约」标签页
- **AI 阈值自学习**:审批/拒绝候选时自动记录评分快照,累计反馈后自动调参
- **弹幕情绪分析**:基于弹幕文本的规则型情绪分析(重复率/感叹号/高情绪梗)
- **流水线进度追踪**:Dashboard 录制状态页新增进度条
- **Dashboard 功能开关**:每直播间「预约录制」「阈值自学习」「弹幕情绪」三项开关,录制中锁定

### 修复

- 超管断流重连优化:重连成功后自动重置退避计数器(backoff→1),避免无谓等待 30s
- ``RecordingSession`` 新增 ``last_reconnected_at`` / ``RECONNECTED`` 状态,仪表盘可见

### 变更

- 版本号同步:`v0.1.1-alpha` → `v0.1.2-alpha`(展示名 `V0.1.2 Alpha`)
  - `pyproject.toml`、`README.md`、`launcher.py` 统一更新
- ``launcher.exe`` 重新编译(版本号 0.1.2,8.1 MB)

## V0.1.1 Alpha (2026-07-02)

### 新增

- **`launcher.exe` 即插即用启动器**:双击即可运行,自动检测系统 Python 3.11+、创建虚拟环境、
  离线安装依赖(`vendor/wheels`)、验证模型与 ffmpeg、启动 Web 管理后台并自动打开浏览器。
  彻底替代 `.ps1`/`.bat` 脚本,规避系统安全策略拦截问题。
  - `launcher.py` — 启动器源码(PyInstaller 编译入口)
  - `build_exe.py` — PyInstaller 一键编译脚本(`--onefile --console`)
  - `launcher.exe` — 编译好的单文件可执行程序(约 8 MB)

### 修复

- 同步主工程 `Recorder.run()` 中 `backoff` 变量未初始化导致 `NameError` 的修复
  (`app/recording/recorder.py` 副本,通过 `build_bundle.py` 重生成时生效)

### 变更

- 版本号同步:`v0.1.0-alpha` → `v0.1.1-alpha`(展示名 `V0.1.1 Alpha`)
  - `pyproject.toml`、`app/__init__.py`(含 `__version_label__`)、`README.md`、`launcher.py` 统一更新
- `README.md` 重构文档结构,推荐 `launcher.exe` 为首选启动方式,脚本列为备用方案
- `.gitignore` 新增排除 PyInstaller 构建临时文件(`build/`、`*.spec`)
- 保留 `.ps1`/`.bat` 脚本作为备用启动方式

## V0.1.0 Alpha (2026-07-01)

### 首次发布

- 自包含分发包:Whisper 模型(`large-v3-turbo`)、ffmpeg、离线 wheel 全部封装在本目录内
- 目标机器无需联网即可运行
- `setup.bat` / `setup.ps1` 全自动一键即用(自动修复 → 离线安装 → 启动)
- `build_bundle.py` 一键打包脚本(在联网机器执行一次即可)
- `check.bat` / `check.ps1` 分发前一键体检
- `manifest.json` 体检自校验清单
