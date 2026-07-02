# Changelog — 即插即用版 (Public)

## V0.1.2.1 Alpha (2026-07-02)

### 新增

- **``launcher.exe`` 全自展开模式**:一个 exe 从零搭建完整运行环境
  - 自动从 GitHub 下载源码（app/config/pyproject.toml/requirements-bundle.txt）
  - 联网安装依赖（清华+阿里云镜像,无需预打包 vendor/wheels）
  - 自动下载 Whisper large-v3-turbo 模型（hf-mirror.com,~1.6GB）
  - 自动下载 ffmpeg/ffprobe（BtbN 静态构建,~80MB）
  - 自动生成 .env 配置文件
  - 所有步骤断点续跑:组件就位则跳过,中断后再次运行接续
- **源码随仓库分发**:``app/``、``config/``、``pyproject.toml`` 现已入库

### 变更

- ``launcher.py`` 完全重写（~650 行 → ~600 行）:集成 GitHub 下载、在线 pip、模型/ffmpeg 下载全流程
- ``launcher.exe`` 重新编译（8.2 MB）
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
