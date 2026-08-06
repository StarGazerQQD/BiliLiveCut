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
# Windows PowerShell / CMD
.\scripts\docker-up.bat

# Linux/macOS
bash scripts/docker-up.sh
```

### 停止

```bash
docker compose -f packaging/docker/compose.yaml down
```

也可以使用仓库已有的停止脚本：

```bash
# Windows PowerShell / CMD
.\scripts\docker-down.bat

# Linux/macOS
bash scripts/docker-down.sh
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

V0.1.17.1 Alpha 使用与本机/Portable 相同的配置真源：默认分片目标为 300 秒，首选 Fun-ASR-Nano，转写整理和高光复核的最大输出预算均为 `65536` token。下播需连续确认 3 次并等待 60 秒，单场默认最长 12 小时；断流恢复仍按 20 次或 180 秒预算收尾。对应配置为 `LIVE_OFFLINE_CONFIRM_COUNT`、`LIVE_SESSION_END_DELAY_S`、`RECORDING_MAX_DURATION_S`、`RECORDING_RECONNECT_MAX_ATTEMPTS` 与 `RECORDING_RECONNECT_MAX_ELAPSED_S`。

控制台按录制场次生成 GMT+8 高光时间线；每个原始分段可识别多个峰值并跨分段取上下文，弹幕默认按 `7.5` 秒接收延迟对齐，成片使用动态入点/出点。房间手工词典、人工校正、阈值反馈和场次重分析与本机/Portable 行为一致，并保留人工审核、边界、草稿和成片。

## 数据持久化

- `./storage/` → 容器内 `/data`（数据库、录制文件、日志等）
- ASR 模型缓存通过 Docker Volume 持久化

## 版本

当前 Docker 发行对应 BiliLiveCut `v0.1.17.1-Alpha`。
