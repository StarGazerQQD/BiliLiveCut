# BiliLiveCut — AI 直播实时切片系统

[![CI](https://github.com/StarGazerQQD/BiliLiveCut/actions/workflows/ci.yml/badge.svg)](https://github.com/StarGazerQQD/BiliLiveCut/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/StarGazerQQD/BiliLiveCut?include_prereleases&sort=semver)](https://github.com/StarGazerQQD/BiliLiveCut/releases)
[![License](https://img.shields.io/github/license/StarGazerQQD/BiliLiveCut)](LICENSE)

**当前版本：V0.1.18.3 Alpha** (`0.1.18.3-alpha`) · [更新记录](CHANGELOG.md)

面向 Bilibili 直播的自动切片工具：**录制 → 转写 → 识别高光 → 人工审核 → 生成切片与文案 → 可选上传**。通过 Web 控制台管理直播间、录制场次、转写、候选和成品。

## 核心功能

- **开播自动录制与分析**：按直播间开启自动化或预约录制，运行期间同步直播间标题。
- **本地多引擎语音处理**：Fun-ASR-Nano、Paraformer、Whisper 负责转写与回退，SenseVoice 提供辅助特征；可选大模型整理正文和生成文案。
- **已有录播分析**：独立页面导入视频与可选 XML、JSON、SRT、ASS 弹幕，预处理后由本地模型转写，再按配置提交 LLM 分析，支持继续上传和失败重试。
- **场次高光时间线**：融合弹幕、音频、语音与趋势信号识别事件，支持跨连续分段的动态剪辑边界。
- **审核与出片**：支持人工修订、多人审核、字幕、封面和后台渲染，并保存任务状态。
- **统一设置与扩展**：Web 设置中心管理业务配置，支持房间级开关、模型服务商和插件。

## 快速开始

### Windows Portable

1. 从 [Releases](https://github.com/StarGazerQQD/BiliLiveCut/releases) 下载 Portable **Full** 或 **Lite**。Full 自带 Python、依赖和 FFmpeg；Lite 需要准备 Python 3.11/3.12、FFmpeg，并在首次安装时下载依赖。
2. 按 [Portable 小白使用说明](packaging/portable/USER_GUIDE_ZH.md) 完成启动和模型准备。两种发行包均不含 ASR 模型，可在线下载，或使用单独的 Engine Pack。
3. 在 Web 控制台添加已获授权的直播间，开始录制；需要无人值守时开启“开播后自动录制并分析”。

Engine Pack 的本地构建与导入方式见 [Portable 构建说明](packaging/portable/README.md)。模型包约 5.5 GiB，GitHub Release 不提供该文件。

### 源码运行

准备 Python 3.11/3.12 和 FFmpeg，在已下载的项目根目录执行以下 PowerShell 命令：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[web,asr-all,llm]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
python -m app.cli init
python -m app.cli serve
```

默认访问 `http://127.0.0.1:8000`。直播间、录制与大部分业务配置都可在 Web 页面管理；安装镜像、CLI 录制和排错见[使用指南](docs/usage.md)。Docker 部署见 [Docker 说明](packaging/docker/README.md)。

## 使用与升级注意事项

- 目前为 **Alpha** 版本。当前数据库校验要求应用版本与结构一致；升级时请保留旧目录，在独立目录重新初始化和配置，程序不会自动迁移旧数据库。
- 自动开播检测需要服务持续运行；程序退出或系统休眠期间不会检测。
- 本地模型处理需要相应计算资源；录直播、采集弹幕及调用远程大模型需要联网。大模型与自动上传均可按需启用。
- Web 业务设置持久化保存，具体生效时间可在设置中心查看。数据库、存储路径和登录身份等启动配置仍通过环境配置管理；远程部署必须设置 `ADMIN_PASSWORD`。
- 仅录制你拥有授权的内容，遵守平台条款。默认手动上传模式只生成待上传材料；启用自动投稿前需配置上传工具和对应开关。

## 文档

| 需要了解 | 入口 |
| --- | --- |
| 下载、首次启动与第一次录制 | [Portable 小白使用说明](packaging/portable/USER_GUIDE_ZH.md) |
| CLI、转写、审核、上传与排错 | [使用指南](docs/usage.md) |
| 已有视频、弹幕导入与智能分析 | [录播导入](docs/recording-import.md) |
| Web 设置、环境变量与生效边界 | [配置参考](docs/configuration.md) |
| 高光事件、评分与剪辑边界 | [热点检测器](docs/hotspot-detector.md) |
| 开发、测试与发行维护 | [开发与维护](docs/development.md) |
| 插件接口与示例 | [插件开发](plugin/README.md) |
| 全部指南与版本历史 | [文档索引](docs/README.md) · [CHANGELOG](CHANGELOG.md) |

## 许可证

BiliLiveCut 项目代码采用 [MIT License](LICENSE)，Copyright (c) 2026 StarGazerQQD。第三方模型及组件适用各自的许可证；项目的 MIT License 不改变任何第三方条款。详见[第三方模型声明](packaging/portable/licenses/THIRD_PARTY_NOTICES.md)。
