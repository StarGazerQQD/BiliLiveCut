# BiliLiveCut — AI 直播实时切片系统

**当前版本:V0.1.3 Alpha** (`0.1.3-alpha`)

针对 Bilibili 直播的全自动工作流:实时录制 → 转写 → 识别高光 → 生成切片 → 生成文案 → (可选)上传。
阶段 1–5 全链路已可用;即插即用分发包见 [`Public/`](Public/README.md)。

> ⚠️ **合规声明**:本项目仅调用 Bilibili 网页播放器自身使用的公开接口,不做任何逆向、破解或绕过平台安全策略的行为。请**仅录制你拥有授权的内容**,遵守平台服务条款与合理访问频率。自动上传默认采用 `manual` 模式(只产出成品与元数据,不调用任何平台接口),零封号风险。

## 功能进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 取流 + FFmpeg 录制 + 60s 分片 + 入库 | ✅ 可用 |
| 2 | Whisper 转写 + 规则/LLM 高光判断 | ✅ 可用 |
| 3 | 自动切片 + 后处理 + 文案 | ✅ 可用 |
| 4 | Web 管理后台 | ✅ 可用 |
| 5 | 上传队列 + Docker 部署 | ✅ 可用 |

## 环境要求

- Python **3.11 / 3.12**(推荐;部分 AI 依赖对 3.13/3.14 的预编译包可能尚未就绪)
- FFmpeg(已加入 PATH,或在 `.env` 指定 `FFMPEG_PATH`)

## 快速开始(Windows PowerShell)

```powershell
cd D:\Vibe\BiliLiveCut

# 1) 创建虚拟环境并安装(阶段1 仅需核心依赖)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

# 2) 准备配置
Copy-Item .env.example .env   # 按需修改

# 3) 初始化数据库
python -m app.cli init

# 4) 登记一个你有授权的直播间(--authorize 表示你确认拥有录制授权)
python -m app.cli add-room "https://live.bilibili.com/你的房间号" --authorize

# 5) 查看 / 检查
python -m app.cli list-rooms
python -m app.cli check 你的房间号     # 只读:看是否开播、可取哪条流

# 6) 开始录制(Ctrl+C 停止)
python -m app.cli record <db_id>       # db_id 见 list-rooms
```

录制产物位于 `storage/raw/session_<id>/`,每 60 秒一个 `.ts` 片段;
片段信息写入 SQLite(`storage/blc.db`),日志位于 `storage/logs/blc.log`。

## 阶段 2:转写 + 高光判断

安装 AI 依赖后即可对片段做语音转写与高光评分:

```powershell
pip install -e ".[asr]"        # faster-whisper(本地转写,免 API 费,境内可用)
pip install -e ".[llm]"        # 可选:大模型复核与文案(OpenAI 兼容,配 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL)

# 对已录制的片段:转写 -> 评分(也可一步 process)
python -m app.cli transcribe <segment_id>
python -m app.cli score <segment_id>
python -m app.cli process <segment_id>
python -m app.cli list-candidates       # 查看高光候选

# 边录边分析(录制 + 实时转写 + 高光评分)
python -m app.cli record <db_id> --pipeline
```

**工作原理与成本控制**:先用零成本规则特征(音量峰值、关键词、语速突增、笑声代理、
弹幕[阶段4接入])算出 `rule_score`;只有超过初筛阈值(`HIGHLIGHT_INIT_THRESHOLD`)
才调用大模型复核(`HIGHLIGHT_THRESHOLD` 控制最终入候选)。未配置 `LLM_API_KEY`
时自动走**纯规则模式**,完全可用、零费用。切片边界会吸附到音频静音处,避免断在词中间。

> **大模型选型(境内)**:系统主要在中国大陆运行,Anthropic/Cursor 系连接不稳定,故 LLM
> 层采用 **OpenAI 兼容协议**,可对接 DeepSeek / 通义千问 / Kimi / 智谱 GLM——只需在 `.env`
> 配 `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`(示例见 `.env.example`)。语音转写始终由
> 本地 Whisper 完成,不依赖联网。

> Whisper 首次运行会自动下载模型(由 `WHISPER_MODEL` 指定,MVP 默认 `small`);
> CPU 下建议 `WHISPER_COMPUTE_TYPE=int8`,GPU 下用 `cuda` + `float16`。

## 阶段 3:自动切片 + 后处理 + 文案

把高光候选生成为可投稿的 MP4 并配好文案:

```powershell
python -m app.cli clip <candidate_id>        # 仅切片(跨片拼接+精剪+后处理+封面)
python -m app.cli copywrite <clip_id>        # 仅生成标题/简介/标签
python -m app.cli produce <candidate_id>     # 一步到位:切片 + 文案
python -m app.cli list-clips                 # 查看成品

# 全自动链路:边录边分析,产生候选后自动出片
python -m app.cli record <db_id> --pipeline --produce
```

**后处理选项**(在 `.env` 配置):响度标准化 `CLIP_LOUDNORM`(默认开)、去首尾静默
`CLIP_REMOVE_SILENCE`、竖屏重构 `CLIP_VERTICAL`(1080x1920 居中黑边)、烧录字幕
`CLIP_SUBTITLE`、最大时长 `CLIP_MAX_DURATION_S`、画质 `CLIP_VIDEO_CRF`。

**切片边界**:按候选的"爆点±留白"起止,跨多个原始片段用 FFmpeg concat 拼接后帧精确
精剪,起止点已在阶段2 吸附到音频静音处,避免断在词中间。

**审核模式**(`live_rooms.mode`):`manual` 成品停在 `reviewing` 待人工发布;`auto`
达标自动置 `ready` 并导出 `storage/ready_to_upload/clip_<id>.json` 清单;`semi` 介于两者。
成品 MP4 留在 `storage/clips/`,清单以路径引用,不复制大文件。

> 未配置 `LLM_API_KEY` 时,文案走**规则回退**(用命中关键词点题 + 转写摘要 +
> 通用标签),全程零费用可用。

## 阶段 4:Web 管理后台

```powershell
pip install -e ".[web]"
python -m app.cli serve              # 默认 http://127.0.0.1:8000
python -m app.cli serve --port 8080 --reload
```

浏览器打开后即可:**添加直播间**、一键**开始/停止录制**(自带实时分析流水线)、查看
**录制状态 / 实时转写 / 候选审核 / 成品切片 / 错误日志**,在线**调整阈值与审核模式**,
对候选**批准出片/拒绝/删除**,对成品**发布(置 ready 并导出待上传清单)/拒绝**,并可页面内预览成品视频。

- 录制以 asyncio 任务在 Web 进程内管理,关闭服务会优雅停止所有录制。
- WARNING 及以上日志会写入 `system_logs`,在"错误日志"标签查看。
- SQLite 已启用 WAL + busy_timeout,缓解录制/分析/Web 并发写竞争。
- 弹幕热度视图为占位(弹幕采集模块待后续接入)。

## 阶段 5:上传队列 + 部署

**可插拔上传器 + 合规优先**:

- 默认 `ManualUploader`:**不调用任何平台接口**,只导出待上传清单到 `storage/ready_to_upload/`,你在 B 站官方渠道手动投稿,**零封号风险**。
- `BiliupUploader`:**默认关闭**,在 Web 后台「上传 / 设置」标签页有一个**开关由你自行决定是否启用**(也可 `python -m app.cli set-upload --biliup`)。启用后需配置 `BILIUP_UPLOAD_CMD`(命令模板)才会真正执行,否则安全失败。⚠ B 站无官方公开投稿 API,biliup 走你自己的登录态,可能违反条款/触发风控/封号,**风险自负**。

**上传模块关闭时**:每场直播结束会**自动弹出切片所在目录**(本机文件管理器打开 `storage/clips/`),Web 端也会弹出提示并显示路径;CLI 录制结束同样会打印并打开目录。

**上传前置校验**:文件完整性、标题/简介合规与长度、内容指纹查重、投稿频率限制(`UPLOAD_MAX_PER_HOUR`),失败重试(`UPLOAD_MAX_RETRIES`),全部记录在 `upload_tasks`。

```powershell
python -m app.cli upload <clip_id>            # 手动触发上传(默认 manual)
python -m app.cli set-upload --biliup --auto  # 开启 biliup + 自动上传(风险自负)
python -m app.cli set-upload --no-biliup      # 关闭(恢复默认 manual)
```

### Docker 部署

```bash
cp .env.example .env          # 按需修改
docker compose up -d          # 构建并启动(镜像内置 FFmpeg)
# 打开 http://localhost:8000
```

运行产物持久化到宿主 `./storage`;Whisper 模型缓存挂载到命名卷 `hf-cache`,避免重建容器重复下载。镜像基于 Python 3.12(对 AI 依赖 wheel 支持最稳)。

## 测试

```powershell
pip install -e ".[dev]"
pytest -q
```

## 排错

| 现象 | 排查 |
|---|---|
| `ffmpeg 不是内部或外部命令` | 安装 FFmpeg 或在 `.env` 设置 `FFMPEG_PATH` 为绝对路径 |
| `check` 显示未开播 | 主播未直播时无流,属正常 |
| 取流报错 / 403 | 部分高清晰度需登录态,可在 `.env` 配置 `BILIBILI_COOKIE`(自担风险) |
| 片段未生成 | 看 `storage/logs/blc.log` 中 `[ffmpeg]` 行;确认流地址可达 |

## 目录结构

见 `app/`(后端主包)、`config/`(权重与关键词)、`storage/`(运行产物,已 gitignore)、`tests/`。
详见设计文档第五步。
