# Changelog — 即插即用版 (Public)

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
