# Changelog — 0.1.1 系列 (已归档)

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

## V0.1.1 Alpha (2026-07-02)

### 新增

- **`launcher.exe` 即插即用启动器**:用户拿到 `Publish-PnP/` 目录后直接双击 `.exe` 即可运行,自动
  检测 Python 环境、创建虚拟环境、离线安装依赖、验证模型与 ffmpeg、启动 Web 管理后台并打开
  浏览器,不再依赖 `.ps1`/`.bat` 脚本,彻底规避系统安全策略拦截问题。
  - `Publish-PnP/launcher.py` — 启动器源码
  - `Publish-PnP/build_exe.py` — PyInstaller 一键编译脚本(`--onefile`)
  - `Publish-PnP/launcher.exe` — 编译好的单文件可执行程序(约 8MB)

### 修复

- 修复 `Recorder.run()` 断流重连循环中 `backoff` 变量未初始化导致 `NameError` 的问题
  (`app/recording/recorder.py` 及 `Publish-PnP/` 副本同步修复)

### 变更

- `.gitattributes` 规范化行尾(LF 入库 / 自动 CRLF Windows 检出),消除跨平台差异噪声
- `.gitignore` 显式添加 `!.env.example` 例外声明,确保配置模板(不含真实密钥)正常入库
- `Publish-PnP/.gitignore` 排除 PyInstaller 构建临时文件(`build/`、`*.spec`)
- `Publish-PnP/README.md` 更新文档,推荐 `launcher.exe` 为首选启动方式
- `Publish-PnP/` 目录版本号与新主工程项目底代码同步至 `v0.1.1-alpha`
