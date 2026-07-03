# BiliLiveCut · 即插即用版（Publish-PnP）

**版本：V0.1.8.1 Alpha** (`0.1.8.1-alpha`)

BiliLiveCut 是一个**全自动 AI 直播切片系统**：监听 Bilibili 直播间 → 实时录制 + 转写 → 识别高光爆点 → 生成剪辑成品 + 文案。

这个 `Publish-PnP/` 目录是**即插即用分发版**：目标电脑只需装好 Python 3.11+，**双击 `launcher.exe`**，剩下的一切（源码、依赖、模型、FFmpeg）自动下载，直接跑到网页控制台。

---

## 系统要求

| 项目 | 最低 | 推荐 |
|------|------|------|
| 操作系统 | Windows 10/11 x64 | Windows 11 |
| Python | 3.11+ | 3.12 / 3.13 |
| 内存 | 8 GB | 16 GB |
| 磁盘 | 10 GB 空闲 | 20 GB（含录制存储） |
| 网络 | 首次启动需联网下载约 2 GB 组件 | — |

---

## 快速开始（零门槛）

### 方式一：双击 `launcher.exe`（推荐，有网）

1. 确保电脑已装 **Python 3.11+**（终端输入 `python --version` 检查）
2. 双击 `launcher.exe`
3. 等待自动部署（首次约 10-30 分钟，视网速而定）：

| 步骤 | 内容 | 大小 | 说明 |
|------|------|------|------|
| ① | 下载源码 | ~200 KB | 从 GitHub 拉取最新 app/config |
| ② | 创建虚拟环境 | — | `.venv` 隔离 Python 依赖 |
| ③ | 安装依赖 | ~500 MB | 阿里云镜像 + 清华镜像(备用) |
| ④ | 下载 Whisper 模型 | ~1.6 GB | large-v3-turbo，最耗时 |
| ⑤ | 下载 FFmpeg | ~80 MB | 无须系统另装 |
| ⑥ | 生成 `.env` 配置 | — | 含合理默认值 |

> **断点续跑**：任何一步失败或中断，再次双击自动从断点继续，已下载的组件不会重复下载。

4. 部署完成后自动打开浏览器 → **Web 管理控制台**（默认 `http://127.0.0.1:8000`）

### 方式二：离线包（给无网机器）

在一台**能联网**的机器上执行一次打包：

```powershell
cd Publish-PnP
pip install huggingface_hub
python build_bundle.py
```

打包完成后把整个 `Publish-PnP/` 目录拷到目标机，双击 `launcher.exe` 即可**离线启动**。

---

## Web 控制台各功能页说明

启动后在浏览器中打开的 Web 控制台包含以下 Tab：

| Tab | 功能 | 前置条件 |
|-----|------|----------|
| **直播间** | 添加房间号、开关录制 | 无 |
| **录制状态** | 当前录制进度、断流/重连状态 | 已添加房间 |
| **录制预约** | 定时自动录制（每日/单次） | 已添加房间 |
| **实时转写** | Whisper 自动转录当前片段 | 录制进行中 |
| **弹幕热度** | 弹幕实时统计、热度曲线 | 已配置 Bilibili Cookie |
| **网感资料库** | 联网采集热点话题/标签 | 已配置大模型 API |
| **候选审核** | 高光片段候选列表、人工审批 | 已完成分析 |
| **成品切片** | 已剪辑的视频、封面、文案 | 已生成切片 |
| **上传 / 设置** | 上传开关、切片目录 | — |
| **模型** | 多 LLM 服务商配置、优先级 | — |
| **账号管理** | 扫码登录 Bilibili、自动采集 Cookie | — |
| **错误日志** | WARNING/ERROR 级别日志 | — |

---

## 配置文件 .env 详解

`launcher.exe` 首次运行会**自动生成**一份含默认值的 `.env`。以下逐段说明每个配置项的含义及何时需要修改。

### 通用

```ini
APP_ENV=dev          # dev=开发(更多日志) / prod=生产
LOG_LEVEL=INFO       # DEBUG / INFO / WARNING / ERROR
```

### 存储

```ini
STORAGE_ROOT=./storage          # 所有产物（录制片段、切片、数据库、日志）的根目录
DATABASE_URL=sqlite:///./storage/blc.db  # SQLite 数据库路径，一般不改
```

### FFmpeg

```ini
FFMPEG_PATH=ffmpeg    # launcher.exe 会自动下载到 bin/ 并注入 PATH
FFPROBE_PATH=ffprobe  # 如系统已装好的 FFmpeg 在 PATH 中，保持默认即可
```

### 录制 / 分片

```ini
SEGMENT_DURATION_S=60            # 每个原始片段的时长（秒），建议 60
PREFERRED_STREAM_PROTOCOL=hls    # 取流协议：hls（稳定）或 flv
STREAM_QUALITY=10000             # 清晰度：10000=原画，400=蓝光，250=超清
RECONNECT_MAX_BACKOFF_S=30       # 断流后最大重试等待秒数
LIVE_POLL_INTERVAL_S=15          # 检查直播间开播/下播的间隔（秒）
COLLECT_DANMAKU=true             # 录制时是否同时采集弹幕（需 Bilibili Cookie）
```

### Bilibili Cookie（弹幕采集必需）

```ini
REQUIRE_AUTHORIZATION=true  # 必须设为 true，确认你有权录制
BILIBILI_COOKIE=            # 登录态 Cookie，填写后可采集弹幕、获取更高清晰度
                             # 推荐通过 Dashboard【账号管理】Tab 扫码登录自动获取
                             # 手动填写格式：DedeUserID=xxx; SESSDATA=xxx; bili_jct=xxx
```

> **重要**：不填 Cookie 时弹幕采集不可用（B 站接口要求登录态），但录制本身不受影响。

### 语音转写（本地 Whisper，无需联网）

```ini
WHISPER_MODEL=./models/whisper-large-v3-turbo  # launcher.exe 自动定位包内模型
WHISPER_DEVICE=cpu                              # cpu（通用）或 cuda（NVIDIA 显卡）
WHISPER_COMPUTE_TYPE=int8                       # int8（CPU 推荐）/ float16（GPU）
```

### 大模型（可选，用于高光复核 / 文案 / 网感采集）

```ini
LLM_PROVIDER=deepseek                # 仅标识，不影响实际连接
LLM_API_KEY=                         # ★ 填入 API Key 才启用大模型；留空走纯规则（零费用）
LLM_BASE_URL=https://api.deepseek.com/v1   # 服务商 base_url，须含版本前缀
LLM_MODEL=deepseek-chat              # 模型标识名
LLM_WEB_SEARCH_PARAM=enable_search   # 联网搜索开关键名（DeepSeek 不支持则自动回退）
LLM_PRICE_INPUT_PER_M=0              # 每百万 token 输入价格（0=不计费）
LLM_PRICE_OUTPUT_PER_M=0             # 每百万 token 输出价格（0=不计费）
LLM_DAILY_BUDGET=0                   # 每日预算上限（0=不限）
```

**多模型配置**：Web 控制台「模型」Tab 可同时添加多个服务商（DeepSeek / 通义千问 / Kimi / 智谱 GLM 等），设置优先级，某个不可用时自动降级到下一个。

### 网感资料库（可选，用于热点采集）

```ini
TREND_ENABLED=false              # 是否启用网感资料库
TREND_API_KEY=                   # 趋势采集专用 API Key（可与 LLM_API_KEY 不同）
TREND_BASE_URL=                  # 趋势采集专用 base_url（留空复用 LLM_BASE_URL）
TREND_MODEL=                     # 趋势采集专用模型（留空复用 LLM_MODEL）
TREND_WEB_SEARCH=true            # 是否开启联网搜索（时效性强）
TREND_MAX_SEARCHES=5             # 单次采集联网搜索次数上限
TREND_MAX_ITEMS=40               # 单次采集入库条目上限
TREND_RETENTION_DAYS=14          # 资料库保留天数
TREND_MATCH_DAYS=7               # 高光评分参考"近期"窗口（天）
```

> 启用后可在「网感资料库」Tab 手动触发采集，或开启「定时采集」每日自动迭代。

### 高光判断阈值

```ini
HIGHLIGHT_INIT_THRESHOLD=0.5     # 进入 LLM 复核的初筛阈值（0-1）
HIGHLIGHT_THRESHOLD=0.65         # 进入候选池的综合评分阈值（0-1）
AUTO_PUBLISH_THRESHOLD=0.85      # 全自动模式下直接发布的阈值（0-1）
```

> 阈值越低越容易切片（更多候选但可能含低质），越高越严格（少而精）。

### 切片后处理

```ini
CLIP_LOUDNORM=true               # 响度标准化（EBU R128），推荐开启
CLIP_REMOVE_SILENCE=false        # 去除首尾静默段
CLIP_VERTICAL=false              # 竖屏重构 1080x1920（适合手机端）
CLIP_SUBTITLE=false              # 烧录字幕（从转写生成 SRT）
CLIP_MAX_DURATION_S=180          # 单个切片最大时长（秒）
CLIP_VIDEO_CRF=20                # x264 画质（0-51，越小越清晰）
CLIP_PRESET=veryfast             # 编码速度：ultrafast / veryfast / medium / slow
```

### 上传

```ini
UPLOADER=manual                  # manual=仅产出文件不投稿（零风险，推荐）
UPLOAD_MAX_RETRIES=3             # 上传失败重试次数
UPLOAD_MAX_PER_HOUR=5            # 每小时投稿上限（频控）
TITLE_MAX_LEN=80                 # 标题长度上限
DESC_MAX_LEN=2000                # 简介长度上限
```

### biliup 上传（社区方案，合规风险自负）

```ini
BILIUP_CONFIG=                              # biliup 配置文件路径（如 cookies.json）
BILIUP_UPLOAD_CMD=                          # 自定义上传命令模板
# 例：BILIUP_UPLOAD_CMD=biliup upload "{file}" --title "{title}" --desc "{desc}"
```

> ⚠ **B 站无面向普通用户的官方公开投稿 API**。biliup 使用你自己的登录态走网页投稿端点，可能违反平台条款、触发风控甚至封号。是否启用由你决定，风险自负。

---

## 目录结构

```
Publish-PnP/
├── launcher.exe               # ★ 双击即用
├── launcher.py                # launcher.exe 的 Python 源码
├── build_exe.py               # 编译 launcher.exe 用的脚本
├── build_bundle.py            # 离线打包脚本（下载模型/依赖/FFmpeg）
├── requirements-bundle.txt    # Python 运行时依赖清单
├── pyproject.toml             # 项目配置（版本号）
│
├── app/                       # 主工程源码（入库，Git 追踪）
├── config/                    # 关键词 / 评分 YAML 配置
├── README_MAIN.md             # 主工程 README（更详细，可选读）
│
├── models/                    # Whisper 模型目录（构建或运行时下载）
│   └── whisper-large-v3-turbo/
├── bin/                       # ffmpeg.exe / ffprobe.exe（构建或运行时下载）
├── vendor/wheels/             # 离线依赖 wheel（构建时下载，给离线机用）
├── .venv/                     # 虚拟环境（launcher.exe 自动创建）
├── storage/                   # ★ 运行产物目录
│   ├── raw/                   #   原始录制片段
│   ├── clips/                 #   成品切片
│   ├── blc.db                 #   SQLite 数据库
│   └── ready_to_upload/       #   待上传清单
└── .env                       # 配置文件（自动生成或手动编辑）
```

---

## 首次使用完整流程

1. **双击 `launcher.exe`** → 等待自动部署完成
2. 浏览器打开 Web 控制台 → **「账号管理」Tab** → 点击「登录」→ 扫码获取 Cookie
3. **「直播间」Tab** → 粘贴直播间链接（如 `https://live.bilibili.com/123456`）→ 勾选授权确认 → 添加
4. 等待主播开播 → 系统**自动检测并开始录制 + 转写**
5. 录制结束后自动分析 → 在 **「候选审核」Tab** 审批高光片段
6. 审批通过后自动生成剪辑 → 在 **「成品切片」Tab** 查看
7. 成品（MP4 + 封面 + 文案）输出在 `storage/clips/` 目录

> **可选增强**：在 `.env` 配置 `LLM_API_KEY` 后，高光复核和文案生成将由大模型辅助（否则走纯规则，同样可用）。

---

## 常用故障排查

| 问题 | 解决 |
|------|------|
| 启动报 `python` 不是命令 | 安装 Python 3.11+，安装时勾选「Add to PATH」 |
| 下载模型卡住不动 | 关闭窗口重新双击，模型支持断点续传 |
| 弹幕采集提示 `code=-352` | 未配置 Bilibili Cookie，去「账号管理」Tab 扫码登录 |
| LLM 调用报错 / 空结果 | 检查 `.env` 中 `LLM_API_KEY` 和 `LLM_BASE_URL` 是否正确 |
| FFmpeg 未找到 | 重新运行 `launcher.exe`，它会检测并自动补充下载 |
| 修改 `.env` 后不生效 | 重启 `launcher.exe` |
| `launcher.exe` 被杀软拦截 | 添加信任白名单 |

---

## 回主工程

此 `Publish-PnP/` 目录是**发布给最终用户的即插即用版本**。如果你需要参与开发或定制，请回到上层目录查看主工程 `README.md` 及 `CHANGELOG.md`。
