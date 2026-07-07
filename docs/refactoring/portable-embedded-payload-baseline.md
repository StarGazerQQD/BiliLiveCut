# Portable Embedded Payload 基线审计

## Phase 0 审计结果

### 关键常量

```text
Source Commit (74c21b4): 74c21b401f1da4ef52f0333c94e3874e80f8ceef
Builder Commit (HEAD):    74c21b401f1da4ef52f0333c94e3874e80f8ceef
Release Version:          0.1.14.5-alpha
```

### 1. 当前 Launcher 源码获取方式

`Publish-PnP/launcher.py` 通过 GitHub API 下载 `main` 分支 zipball:

```python
ARCHIVE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/zipball/main"
```

回退镜像: `gh-proxy.com`, `ghproxy.net`

**问题**: 没有内置 Payload，首次运行必须联网访问 GitHub。

### 2. 当前 PyInstaller 参数

`build_exe.py`: `--onefile --console`，仅嵌入 `launcher.py`，无源码 Payload。

### 3. 当前源码提取方式

`build_bundle.py`: 使用 `shutil.copytree` 从当前工作区 (`PROJECT_ROOT`) 复制源码:

```python
SOURCE_ITEMS = ["app", "config", "pyproject.toml"]
```

**问题**: 直接复制当前工作区，可能混入未提交修改或 HEAD 之后的代码。

### 4. 关键发现

| 项目 | 状态 |
|------|------|
| 源码从 GitHub 下载 | 是 (zipball/main) |
| GitHub 代理回退 | gh-proxy.com, ghproxy.net |
| 内置 Payload | 无 |
| PyInstaller 嵌入资源 | 仅 launcher.py |
| Python 检测 | _find_system_python() |
| venv 创建 | subprocess venv |
| 依赖安装 | 阿里云+清华镜像 |
| FFmpeg 下载 | GitHub BtbN releases (带镜像回退) |
| 模型下载 | hf-mirror.com |
| .env 位置 | 工作目录根 |
| requirements-bundle.txt | 不存在于 Publish-PnP 目录 |
| Launcher 硬编码版本 | V0.1.12.9 Alpha |
| README 显示版本 | V0.1.12.9 Alpha |
| 构建输出位置 | Publish-PnP/launcher.exe |
| 源码来源 | 当前工作区复制 (shutil.copytree) |
| Commit 74c21b4 可解析 | 是 |
| packaging/ 目录存在 | 否 |
