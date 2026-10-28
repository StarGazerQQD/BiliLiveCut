# BiliLiveCut — AI 直播实时切片系统

[![CI](https://github.com/StarGazerQQD/BiliLiveCut/actions/workflows/ci.yml/badge.svg)](https://github.com/StarGazerQQD/BiliLiveCut/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/StarGazerQQD/BiliLiveCut?include_prereleases&sort=semver)](https://github.com/StarGazerQQD/BiliLiveCut/releases)
[![License](https://img.shields.io/github/license/StarGazerQQD/BiliLiveCut)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-online-00AEEC?logo=readthedocs&logoColor=white)](https://stargazerqqd.github.io/BiliLiveCut/)

**当前版本：V0.1.20.2 Alpha** (`0.1.20.2-alpha`) · [更新记录](https://stargazerqqd.github.io/BiliLiveCut/releases/current.html)

面向 Bilibili 直播的自动切片工具：**录制 → 转写 → 识别高光 → 人工审核 → 生成切片与文案 → 可选上传**。通过 Web 控制台管理直播间、录制场次、转写、候选和成品。

## 核心功能

- **开播自动录制与分析**：按直播间开启自动化或预约录制，运行期间同步直播间标题。
- **本地多引擎语音处理**：Fun-ASR-Nano、Paraformer、Whisper 负责转写与回退，SenseVoice 提供辅助特征；也可显式选择 OpenAI 兼容远程转写；可选大模型整理正文和生成文案。
- **已有录播分析**：独立页面批量选择视频与可选 XML、JSON、SRT、ASS 弹幕，按文件名配对并顺序上传，预处理后由所选 ASR 引擎转写，再按配置提交 LLM 分析，支持继续上传和失败重试。
- **弹幕烧录**：独立于 ASR 字幕，支持滚动、顶部与下方样式及字号、颜色、不透明度和密度设置，按场次时间轴用于主片和派生版本。
- **场次高光时间线**：融合弹幕、音频、语音与趋势信号识别事件，支持跨连续分段的动态剪辑边界。
- **证据与编辑判断**：分别展示内容解释、推荐/拒绝/待复核和技术分数；管理员可预览并重评已有对象，保留人工操作。支持只读导出与独立标注评估，详见[评估流程](docs/development/editorial-evaluation.md)。
- **审核与出片**：支持人工修订、多人审核、字幕、封面和后台渲染，并保存任务状态。
- **统一设置与扩展**：Web 设置中心管理业务配置，支持房间级开关、模型共享连接、独立业务路由和插件；配置按即时读取、安全边界或重启生效。
- **直播源插件**：通过正式契约接入其他平台，共用监控、录制和分析流水线；平台取流由独立插件提供，弹幕能力可选。

## 快速开始

### Windows Portable

需要 NVIDIA GPU 转写时，先按 [CUDA 安装与实际设备确认](https://stargazerqqd.github.io/BiliLiveCut/features/asr.html#cuda-安装与实际设备确认)安装匹配的运行时，再选择 ASR 设备；只修改 `.env` 为 `cuda` 不会安装 CUDA 版 PyTorch。

1. 从 [Releases](https://github.com/StarGazerQQD/BiliLiveCut/releases) 下载 Portable **Full** 或 **Lite**。Full 自带 Python、依赖和 FFmpeg；Lite 需要准备 Python 3.11/3.12、FFmpeg，并在首次安装时下载依赖。
2. 按 [Portable 小白使用说明](https://stargazerqqd.github.io/BiliLiveCut/guide/portable.html) 完成启动和模型准备。两种发行包均不含 ASR 模型，可在线下载，或使用单独的 Engine Pack。
3. 在 Web 控制台添加已获授权的直播间，开始录制；需要无人值守时开启“开播后自动录制并分析”。

Engine Pack 的本地构建与导入方式见 [Portable 构建说明](https://stargazerqqd.github.io/BiliLiveCut/development/portable.html)。模型包约 5.5 GiB，GitHub Release 不提供该文件。

### 源码运行

准备 FFmpeg 和 Node.js 24，在已下载的项目根目录选择一种 Python 安装方式。C/Cython 扩展需要编译器与 Python 开发头文件，准备方法见[开发说明](https://stargazerqqd.github.io/BiliLiveCut/development/environment.html#原生扩展构建与安装排错)。

使用 [uv](https://docs.astral.sh/uv/getting-started/installation/)（默认 Python 3.12），以下 PowerShell 命令安装运行依赖和全部 ASR、LLM 后端：

```powershell
uv sync --locked --no-dev --extra web --extra asr-all --extra llm
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv run --locked --no-dev --extra web --extra asr-all --extra llm blc init
npm --prefix frontend ci
npm --prefix frontend run build
uv run --no-sync blc start
```

仅录制和使用 Web 后台时可以省略 `--extra asr-all --extra llm`，后续 `uv run` 也使用同样的 extras。开发环境直接执行 `uv sync --locked`，详见[开发说明](https://stargazerqqd.github.io/BiliLiveCut/development/environment.html)。

使用传统 venv + pip（Python 3.11/3.12），安装方式继续保留：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[web,asr-all,llm]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
blc init
npm --prefix frontend ci
npm --prefix frontend run build
blc start
```

默认访问 `http://127.0.0.1:8000`。Click 命令入口为 `blc`；`start` 启动独立静态前端与 FastAPI API 两个进程，只运行后端 API 使用 `blc backend`，只运行已构建的前端使用 `blc frontend --backend http://127.0.0.1:8000`（默认前端端口 8080）。前端开发和热更新见 [Web 页面开发](https://stargazerqqd.github.io/BiliLiveCut/development/frontend.html)。直播间、录制与大部分业务配置都可在 Web 页面管理；安装镜像、CLI 录制和排错见[使用指南](https://stargazerqqd.github.io/BiliLiveCut/features/)。Docker 部署见 [Docker 说明](https://stargazerqqd.github.io/BiliLiveCut/guide/docker.html)。

## 使用与升级注意事项

- **应用版本与数据库格式独立管理。** 升级前停止服务并备份运行目录；Schema 6、模型指纹、实际结构、约束和完整性全部相符时，可以复用已配置的数据库，创建来源版本保持原样。不兼容格式会拒绝启动，不自动迁移、删除或替换；先在副本上核查受支持的转换方案，或显式使用独立新库。配置按当前 `.env.example` 核查，不恢复已移除的别名或接口；运行时由新版本重新准备。模型内容可独立复用：支持 schema 5 Engine Pack 及 schema 7 安装清单，发行版本或源码提交不同仅提示来源差异，仍须通过当前模型仓库、revision、格式和文件完整性检查；不支持的 schema 不会自动升级。降级需另行核查目标版本对配置和业务记录的支持，不能只凭 Schema 编号判断。
- 自动开播检测需要服务持续运行；程序退出或系统休眠期间不会检测。
- 本地模型处理需要相应计算资源；录直播、采集弹幕、远程转写及调用远程大模型需要联网。大模型与自动上传均可按需启用。
- Web 业务设置持久化保存，具体生效时间可在设置中心查看。数据库、存储路径和登录身份等启动配置仍通过环境配置管理；源码/Portable 远程部署必须设置 `ADMIN_PASSWORD`；Docker 可通过一次性初始化码在网页设置管理员密码。
- 仅录制你拥有授权的内容，遵守平台条款。默认手动上传模式只生成待上传材料；启用自动投稿前需配置上传工具和对应开关。

## 文档

按任务浏览[文档导览](https://stargazerqqd.github.io/BiliLiveCut/guide/)；新用户先看[安装方式](https://stargazerqqd.github.io/BiliLiveCut/guide/installation.html)。文档站的本地预览与 GitHub Pages 配置见[维护说明](https://stargazerqqd.github.io/BiliLiveCut/development/documentation.html)。

| 需要了解 | 入口 |
| --- | --- |
| 下载、首次启动与第一次录制 | [Portable 小白使用说明](https://stargazerqqd.github.io/BiliLiveCut/guide/portable.html) |
| CLI、转写、审核、上传与排错 | [使用指南](https://stargazerqqd.github.io/BiliLiveCut/features/) |
| 已有视频、弹幕导入与智能分析 | [录播导入](https://stargazerqqd.github.io/BiliLiveCut/features/imports.html) |
| Web 设置、环境变量与生效边界 | [配置参考](https://stargazerqqd.github.io/BiliLiveCut/guide/configuration.html) |
| 高光事件、评分与剪辑边界 | [热点检测器](https://stargazerqqd.github.io/BiliLiveCut/reference/hotspot-detector.html) |
| 开发、测试与发行维护 | [开发与维护](https://stargazerqqd.github.io/BiliLiveCut/development/environment.html) |
| 插件接口与示例 | [插件开发](https://stargazerqqd.github.io/BiliLiveCut/development/plugins.html) |
| 直播源契约、平台身份及房间登记 | [直播源插件](https://stargazerqqd.github.io/BiliLiveCut/development/live-sources.html) |
| 全部指南与版本历史 | [文档索引](https://stargazerqqd.github.io/BiliLiveCut/guide/) · [CHANGELOG](https://stargazerqqd.github.io/BiliLiveCut/releases/current.html) |

## 鸣谢

感谢每一位提供反馈、报告问题及支持本项目的朋友。

- GitHub：[shuangyiliu211126com](https://github.com/shuangyiliu211126com)（[#61](https://github.com/StarGazerQQD/BiliLiveCut/issues/61)、[#62](https://github.com/StarGazerQQD/BiliLiveCut/issues/62)）
- GitHub：[xiu1zi3](https://github.com/xiu1zi3)（[#60](https://github.com/StarGazerQQD/BiliLiveCut/issues/60)）
- GitHub：[BEIJUUUUU](https://github.com/BEIJUUUUU)（[#68 定位反馈](https://github.com/StarGazerQQD/BiliLiveCut/issues/68#issuecomment-6108960218)，提供镜像重定向和 SDK 兼容性复现）
- GitHub：[GEYUANwuqi](https://github.com/GEYUANwuqi)（[PR #73](https://github.com/StarGazerQQD/BiliLiveCut/pull/73)，贡献 uv 支持、pre-commit 钩子及 [Docker 部署修复](https://github.com/StarGazerQQD/BiliLiveCut/pull/78)）
- GitHub：[Aethey](https://github.com/Aethey)（[#85](https://github.com/StarGazerQQD/BiliLiveCut/issues/85)，反馈直播线路连接及重连问题）

- GitHub：[2335181](https://github.com/2335181)（[#88](https://github.com/StarGazerQQD/BiliLiveCut/issues/88)，反馈直播间删除入口）

- Bilibili：[大口径喷子](https://space.bilibili.com/13886027)
- Bilibili：[口米_K-Mi](https://space.bilibili.com/493488643)
- Bilibili：[_铃子](https://space.bilibili.com/95763745)
- Bilibili：[爱莉希雅office](https://space.bilibili.com/11241685)
- Bilibili：[七圣小伞](https://space.bilibili.com/330443150)
- Bilibili：[溯园](https://space.bilibili.com/22335588)
- Bilibili：[桜墨Moku](https://space.bilibili.com/6432607)
- Bilibili：[TDD-怡汁汽水糖](https://space.bilibili.com/1137908003)
- Bilibili：[伯丶牙](https://space.bilibili.com/21959755)

## 许可证

BiliLiveCut 项目代码采用 [MIT License](LICENSE)，Copyright (c) 2026 StarGazerQQD。第三方模型及组件适用各自的许可证；项目的 MIT License 不改变任何第三方条款。详见[第三方模型声明](https://stargazerqqd.github.io/BiliLiveCut/releases/licenses.html)。
