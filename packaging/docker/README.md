# BiliLiveCut Docker 部署

本目录包含 BiliLiveCut 的 Docker 容器化发行文件。

## 文件说明

| 文件 | 说明 |
|------|------|
| `Dockerfile` | 基于 Python 3.12-slim 的容器镜像定义 |
| `compose.yaml` | Docker Compose 编排文件（构建上下文为仓库根目录） |

## 使用方式

### 快速启动

```bash
# 从仓库根目录运行
docker compose -f packaging/docker/compose.yaml up --build
```

### 便捷脚本

项目根目录提供了便捷启动脚本：

```bash
# Windows PowerShell
.\scripts\docker-up.ps1

# Linux/macOS
bash scripts/docker-up.sh
```

### 停止

```bash
docker compose -f packaging/docker/compose.yaml down
```

### 镜像说明

- 基础镜像：`python:3.12-slim`
- 自动安装 FFmpeg
- 以非 root 用户 `appuser` 运行
- 数据持久化到宿主 `./storage` 目录
- 默认启动 Web 控制台，监听 `http://localhost:8000`

## 构建上下文

> **重要**：Docker 构建上下文为**仓库根目录**，`.dockerignore` 也位于根目录。
> 不得将 `.dockerignore` 移动到 `packaging/docker/`。

```yaml
build:
  context: ../..              # 仓库根目录
  dockerfile: packaging/docker/Dockerfile
```

## 配置

通过仓库根目录的 `.env` 文件配置：

```bash
cp .env.example .env
# 编辑 .env 填入必要配置
```

## 数据持久化

- `./storage/` → 容器内 `/data`（数据库、录制文件、日志等）
- ASR 模型缓存通过 Docker Volume 持久化

## 版本

当前 Docker 发行对应 BiliLiveCut `v0.1.15-alpha`。
