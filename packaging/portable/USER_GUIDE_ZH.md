# BiliLiveCut Portable 小白使用说明

适用版本：`v0.1.17.1-Alpha` · 适用系统：Windows 10/11 x64

这份说明面向不懂 Python、Git 或命令行的普通 Windows 用户。按顺序操作即可完成下载安装、首次启动、基础配置、添加直播间和首次录制。

> BiliLiveCut 只能用于你拥有录制和使用授权的内容。不要录制、剪辑或传播未经授权的直播内容。

## 先看结论

- 新手请选择 **Full 完整版 ZIP**，不要先用 Lite 单 EXE。
- Full 已包含 Python、程序依赖、FFmpeg 和 FFprobe，不需要自己安装开发工具。
- Full **不包含语音模型**。如果没有单独取得匹配版本的 Engine Pack，首次启动仍需联网下载约 5.5 GB 模型。
- 程序运行时会出现一个黑色 Launcher 窗口。使用期间不要关闭它；关闭后 Web 控制台和录制都会停止。
- 服务启动后访问 <http://127.0.0.1:8000>。当前版本不保证自动打开浏览器。
- “账号管理”会优先调用电脑已安装的 Google Chrome；没有 Chrome 时会自动下载一次 Playwright Chromium。
- 第一次测试先完成一小段授权直播录制，并在 `storage/raw/` 找到文件。Cookie、大模型 API 和自动上传都不是首次使用的必需项。

## V0.1.17.1 Alpha 先知道的新流程

- “候选审核”首页升级为“场次时间线”。每场录制按 GMT+8 展示高光节点、摘要、1～2 条代表弹幕、置信度和来源依据；点击“审片”仍可播放、调整边界、批准或拒绝。
- 一个五分钟原始分段最多分析 4 个分散窗口，除音频峰值外也覆盖低声量语义事件；高光可跨相邻分段。系统会等相邻分段转写可用后再分析靠近断点的内容，并至少保留 30 秒后文，不再固定输出 1 分 30 秒。
- Bilibili 弹幕默认向前校正 `7.5` 秒来对齐画面，同场过近且内容相似的节点会去重，减少一个爆点重复出片。
- “实时转写”可以人工改正文，并可填写“错误词=正确词”加入该直播间的专属词典；保存后可自动重分析本场。
- 修改阈值后使用“按新阈值重分析”；修改房间词典或 ASR 模型后使用“按新词典/模型重新转写”。人工审核、手工边界、草稿、成片和人工正文不会被静默覆盖。
- 主播下播需连续确认 3 次，再等待 60 秒收尾；期间恢复直播会取消停止。单场默认最长 12 小时，断流仍受 20 次或 180 秒的恢复预算限制；预算耗尽后要等房间真实离线，才允许下一次开播自动录制。
- 五秒轮询会保留转写纠错、房间词典、房间配置、功能开关和下拉选择中的未保存内容，也会记住原始 ASR、已学习词典及时间线评分详情的展开状态。
- 人工提交审核后，任务不会继续显示 `awaiting_review`；“独立成片”会进入受心跳保护的后台渲染，完成后出现在成品切片。

## 1. 电脑和网络要求

| 项目 | 最低要求 | 推荐配置 |
|---|---|---|
| 操作系统 | Windows 10/11 64 位 | Windows 11 64 位 |
| CPU | 支持 64 位 Windows 的现代 CPU | 近几年 6 核及以上 CPU |
| 内存 | 8 GB | 16 GB 或更多 |
| 可用磁盘 | 15 GB，仅够安装和短时测试 | 30 GB 以上；长期录制建议准备独立大容量磁盘 |
| 显卡 | 不要求独立显卡，默认使用 CPU | NVIDIA 显卡可用于后续加速调试，但首次测试保持 CPU 默认值 |
| 网络 | 下载发行包、下载模型和访问 Bilibili 时需要 | 稳定宽带；首次模型下载流量约 5.5 GB |
| 浏览器 | Edge、Chrome 等现代浏览器可访问控制台 | 推荐安装最新版 Google Chrome，账号登录可直接复用 |

Full 版不要求系统安装 Python、FFmpeg、Visual Studio、Git 或其他编程工具。

以下位置不适合作为程序目录：

- `C:\Program Files`、`C:\Windows` 等需要管理员权限的目录；
- OneDrive 等正在自动同步的目录；
- 临时目录、压缩包预览窗口和网络共享盘；
- FAT32 U 盘。大文件可能超过 FAT32 的单文件限制。

推荐使用 `D:\BiliLiveCut`；没有 D 盘时可使用 `C:\BiliLiveCut`。

## 2. 下载正确的文件

打开项目的 [GitHub Releases 页面](https://github.com/StarGazerQQD/BiliLiveCut/releases)，进入 `v0.1.17.1-Alpha`，下载：

1. `BiliLiveCut-Portable-Full-0.1.17.1-alpha-x64.zip`
2. `SHA256SUMS.txt`

不要把下面这些文件当成 Windows 小白版：

- `BiliLiveCut-Portable-Lite-*.exe`：Lite 版，需要系统 Python 3.11/3.12，并依赖联网安装组件；
- `.whl` 或 `.tar.gz`：面向 Python 开发者；
- `full-build-manifest.json`、`lite-build-manifest.json`：构建清单，不是启动程序。

## 3. 校验下载文件

校验可以确认文件没有下载损坏，也能避免误用来源不明的文件。

1. 把 Full ZIP 和 `SHA256SUMS.txt` 放在同一个下载目录。
2. 在该目录空白处按住 `Shift` 并单击鼠标右键，选择“在终端中打开”。
3. 复制并执行：

```powershell
Get-FileHash ".\BiliLiveCut-Portable-Full-0.1.17.1-alpha-x64.zip" -Algorithm SHA256
```

4. 将输出的 `Hash` 与 `SHA256SUMS.txt` 中同名文件前面的值比较。英文字母大小写不同不影响结果。
5. 两者必须完全一致。若不一致，删除 ZIP 并从 Releases 页面重新下载；不要继续解压或运行。

## 4. 正确解压 Full ZIP

1. 新建目录，例如 `D:\BiliLiveCut`。
2. 右键 ZIP，选择“全部解压”。
3. 打开解压出来的 `BiliLiveCut-Portable-Full-0.1.17.1-alpha-x64` 文件夹。
4. 确认同一层能看到：

```text
BiliLiveCut-Portable.exe
portable-python\
vendor\wheels\
bin\ffmpeg.exe
bin\ffprobe.exe
README.txt
LICENSE.txt
```

必须保留整个目录结构。不要只把 `BiliLiveCut-Portable.exe` 单独拖到桌面，也不要直接在 ZIP 预览窗口里双击运行。

## 5. 准备模型

Full 版包含运行环境，但不包含四个语音识别模型。

### 情况 A：没有 Engine Pack

这是普通测试者最常见的情况。保持网络连接，首次启动时 Launcher 会从 ModelScope 和 Hugging Face 下载全部模型。模型总量约 5.5 GB，可能需要较长时间。

### 情况 B：分发者提供了 Engine Pack

只接受与应用版本匹配的文件：

```text
BiliLiveCut-EnginePack-0.1.17.1-alpha.zip
```

将这个 ZIP 原样放到 `BiliLiveCut-Portable.exe` 同级目录，不要手动解压。Launcher 会先做完整性校验，再安装到 `models/`。

不要从不明网盘或陌生人处下载模型包。Engine Pack 校验失败时，程序会拒绝使用它并尝试在线下载完整模型。

## 6. 第一次启动

1. 双击 `BiliLiveCut-Portable.exe`。
2. 如果 Windows SmartScreen 弹出提醒，先确认文件来自本项目 Releases 且 SHA-256 已校验，再选择“更多信息”查看是否允许运行。不要为了运行程序而关闭整个杀毒软件。
3. 黑色 Launcher 窗口会依次执行：

```text
[1/6] 安装内置业务源码
[2/6] 创建 .env 配置
[3/6] 创建 Python 虚拟环境
[4/6] 从 vendor/wheels 离线安装依赖
[5/6] 校验或下载四引擎模型
[6/6] 启动 Web 控制台
```

4. 首次运行不要关闭窗口。Full 的 Python 依赖来自本地 wheelhouse，不会回退到 PyPI 镜像；模型仍可能需要联网下载。
5. 出现类似下面的文字后，服务已经启动：

```text
Starting Web console...
http://127.0.0.1:8000
```

6. 打开 Edge 或 Chrome，在地址栏输入：

```text
http://127.0.0.1:8000
```

如果页面没有自动弹出，手动输入地址即可。

## 7. 首次使用前的基础配置

第一次测试不需要填写 Cookie 或购买大模型 API。建议先保持最少配置。

### 7.1 找到并编辑 `.env`

首次启动后，程序目录会自动生成 `.env`。

1. 先在黑色窗口中按 `Ctrl+C`，等待服务停止。
2. 在程序目录找到 `.env`，右键选择“打开方式”→“记事本”。
3. 每个配置占一行，只修改等号右侧，不要删除变量名。
4. 保存后重新双击 `BiliLiveCut-Portable.exe`。

`.env` 可能包含密码、Cookie 和 API Key，不要截图公开，也不要发送给其他人。

### 7.2 第一次测试建议值

```ini
APP_ENV=prod
LOG_LEVEL=INFO
ADMIN_PASSWORD=
STORAGE_ROOT=./storage
STREAM_QUALITY=10000
SEGMENT_DURATION_S=300
LIVE_OFFLINE_CONFIRM_COUNT=3
LIVE_SESSION_END_DELAY_S=60
RECORDING_MAX_DURATION_S=43200
RECORDING_RECONNECT_MAX_ATTEMPTS=20
RECORDING_RECONNECT_MAX_ELAPSED_S=180
COLLECT_DANMAKU=true
DANMAKU_LOGIN_RETRY_MAX_ATTEMPTS=5
DANMAKU_LOGIN_RETRY_INTERVAL_S=60
RECORDING_PIPELINE_ENABLED=true
ASR_PRIMARY=funasr_nano
ASR_VAD_MAX_SEGMENT_S=30
ASR_TASK_MAX_CONCURRENCY=1
TRANSCRIPT_LLM_REFINE_ENABLED=true
TRANSCRIPT_LLM_REFINE_MAX_TOKENS=65536
HIGHLIGHT_LLM_MAX_TOKENS=65536
HIGHLIGHT_INIT_THRESHOLD=0.28
HIGHLIGHT_THRESHOLD=0.38
HIGHLIGHT_REVIEW_THRESHOLD=0.32
HIGHLIGHT_AUTO_APPROVE_THRESHOLD=0.72
HIGHLIGHT_MAX_CANDIDATES_PER_SEGMENT=4
HIGHLIGHT_PEAK_MIN_DISTANCE_S=25
HIGHLIGHT_MIN_PRE_ROLL_S=20
HIGHLIGHT_MIN_POST_ROLL_S=30
DANMAKU_EVENT_LAG_S=7.5
AUTO_PUBLISH_THRESHOLD=0.80
LLM_API_KEY=
TREND_ENABLED=false
UPLOADER=manual
```

说明：

- `ADMIN_PASSWORD` 留空：只允许本机通过 `127.0.0.1` 使用，首次测试最简单。
- `STORAGE_ROOT` 保持 `./storage`：数据库、原始录像和成品都在程序目录内，便于备份。
- `STREAM_QUALITY=10000`：请求原画；若匿名访问无法取得，可改为 `400` 或 `250`。
- `SEGMENT_DURATION_S=300`：每个原始片段以 5 分钟为目标；FFmpeg 按关键帧落盘，实际时长允许小幅浮动。
- `LIVE_OFFLINE_CONFIRM_COUNT=3` 与 `LIVE_SESSION_END_DELAY_S=60`：连续确认三次下播后等待 60 秒再收尾；等待期间恢复直播会撤销停止。
- `RECORDING_MAX_DURATION_S=43200`：单场最长 12 小时；达到后安全收尾，若直播仍在继续则由自动监控开启下一场会话。
- `RECORDING_RECONNECT_MAX_ATTEMPTS=20` 与 `RECORDING_RECONNECT_MAX_ELAPSED_S=180`：连续无法恢复取流时，达到 20 次或 180 秒中的任一上限即自动结束本场；若 Bilibili 仍报告直播中，程序会等待一次真实离线再允许下一场自动启动。成功产出新片段后两项预算归零。某项设为 `0` 只禁用该项，两项都为 `0` 会无限重试，不建议无人值守时使用。
- `COLLECT_DANMAKU=true`：采集公开弹幕；有 Cookie 时登录优先，失败后立即匿名兜底。
- `DANMAKU_LOGIN_RETRY_MAX_ATTEMPTS=5`：单场最多累计 5 次登录失败（首次计入）；达到后本场固定匿名，设为 `0` 可始终匿名。
- `DANMAKU_LOGIN_RETRY_INTERVAL_S=60`：匿名连接存活期间，每 60 秒后台探测一次登录链路。
- 若日志出现 `code=-352`，表示请求触发平台风控，不能只据此判定 Cookie 过期。登录请求失败会匿名兜底；匿名请求也失败时会按配置间隔重试，录像与实时转写仍会继续。
- `RECORDING_PIPELINE_ENABLED=true`：新开始或恢复的录制默认实时转写；也可在“配置 → 功能开关”中修改，下一次录制生效。
- `ASR_PRIMARY=funasr_nano`：优先使用本地 Fun-ASR-Nano；无有效输出时自动回退 Paraformer，再回退 Whisper。
- `ASR_VAD_MAX_SEGMENT_S=30`：先把 TS 标准化为 16 kHz 单声道 WAV，再由 FSMN-VAD 把 Nano 的单句限制在 30 秒；建议保持默认值。
- `ASR_TASK_MAX_CONCURRENCY=1`：同时转写的录制分段数。CUDA 可在“配置 → 功能开关”中逐步提高到 2～8，但每个并行线程会加载独立模型，显存占用通常近似线性增加；CPU 或显存不足时保持 1。
- `TRANSCRIPT_LLM_REFINE_ENABLED=true`：每个切片完成 ASR 后，用已配置的大模型整理正文并生成概括；失败时保留原始 ASR，不中断分析。
- `TRANSCRIPT_LLM_REFINE_MAX_TOKENS=65536` 与 `HIGHLIGHT_LLM_MAX_TOKENS=65536`：分别控制五分钟转写整理和高光复核的最大输出预算；实际消耗由模型响应决定，可按服务商能力调低。
- 高光默认初筛/候选/人工审核阈值为 `0.28/0.38/0.32`，自动批准/发布阈值为 `0.72/0.80`。阈值越低越容易产生候选；房间已经保存的显式值不会被升级覆盖。
- `HIGHLIGHT_MAX_CANDIDATES_PER_SEGMENT=4` 与 `HIGHLIGHT_PEAK_MIN_DISTANCE_S=25`：每个原始分段最多分析 4 个分散窗口；真实峰值优先，不足时补充均匀探针，相邻窗口至少间隔 25 秒，跨分段分析仍按场次统一去簇。
- `HIGHLIGHT_MIN_PRE_ROLL_S=20` 与 `HIGHLIGHT_MIN_POST_ROLL_S=30`：动态切片至少保留的前文和后文，LLM 与静音吸附仍可继续向外扩展。活动直播会等待下一相邻分段完成转写，避免断点附近的后半段尚未落盘就提前定界。
- `DANMAKU_EVENT_LAG_S=7.5`：评分时把弹幕信号向前对齐 7.5 秒，不修改数据库中的原始弹幕时间。
- `LLM_API_KEY` 留空：转写梳理自动跳过，高光判断使用本地规则评分，不产生 API 费用。
- `TREND_ENABLED=false`：不启用联网热点采集。
- `UPLOADER=manual`：只生成本地文件，不自动投稿。

若历史版本已经写入明显重复的转写，可在“实时转写”页点击“重新识别”。V0.1.17.1 的场次重分析会保留人工审核、手工边界、草稿、成片、反馈与人工正文，只重建可安全重建的自动结果；有活动任务时会等待或显示明确原因。

### 7.3 不要先改这些配置

首次测试请保持 ASR 设备为默认 CPU、任务并行数为 1，不要直接改为 CUDA：

```ini
ASR_PRIMARY_DEVICE=cpu
ASR_AUXILIARY_DEVICE=cpu
ASR_REVIEW_DEVICE=cpu
ASR_FALLBACK_DEVICE=cpu
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
ASR_TASK_MAX_CONCURRENCY=1
```

先证明录制链路正常，再单独测试 GPU。启用 CUDA 后可逐步把“ASR 分段并行数”从 1 提高；每提高一个并行任务都会增加一套模型实例，错误的 CUDA 配置或过高并行可能导致模型加载失败或显存不足。

## 8. 添加直播间并完成第一次录制

请选用你自己或已明确取得授权、并且当前正在直播的房间做短时测试。

1. 保持黑色 Launcher 窗口开启，并打开 <http://127.0.0.1:8000>。
2. 进入“直播间”页面。
3. 在“添加直播间”中输入完整链接或数字房间号，例如：

```text
https://live.bilibili.com/123456
```

4. 勾选“我已确认拥有录制授权”。未勾选时系统会拒绝添加。
5. 点击“添加”。
6. 打开“功能开关”，找到刚添加的直播间；首次测试可只开启“自动分析”，确认录制和分析后再逐项开启自动渲染、自动审核与自动上传。
7. 点击“保存本直播间开关”。预约录制、阈值自学习、弹幕情绪、审核阈值和 ASR 手工词典也在这里按直播间独立设置；热词一行一个。
8. 在房间卡片中点击“开始录制”。
9. 打开“录制状态”，确认状态变为 `recording`，片段数量开始增加。
10. 先录制 2～5 分钟，再回到“直播间”点击“停止录制”。
11. 在程序目录检查：

```text
storage\raw\session_<数字>\
```

目录中出现 `.ts` 片段，表示从安装到录制的第一条链路已经成功。

> “功能开关”中的五项流水线设置按直播间独立生效。关闭某一项只会阻止后续自动推进，仍可在对应页面手动处理；房间“自动上传”还必须配合“上传与发布”页的全局上传总开关。

> 编辑直播间录制选项或独立功能开关时，页面会显示“有未保存修改”，并暂停对应表单的五秒自动重绘；输入中的草稿不会被后台轮询覆盖，开始、停止或恢复录制后的状态、会话号和操作按钮仍会立即更新。保存后恢复完整刷新。

## 9. 弹幕登录回退、Cookie 和高清访问

Cookie 不是公开直播录制或弹幕采集的必需项。程序会按网页 WBI 请求格式获取短期 token：存在 Cookie 时先尝试登录请求并使用 `DedeUserID` 鉴权；接口业务错误或登录鉴权被拒绝后，立即改用无 Cookie、`uid=0` 的匿名链路。匿名连接保持工作时会按配置间隔探测登录恢复，达到单场失败上限后不再尝试 Cookie。平台允许的部分高清晰度仍可能需要登录态。

- 首次测试可保持 `COLLECT_DANMAKU=true`，在“弹幕热度”页确认已经收到数据。
- 点击“账号管理”→“登录”后，程序会优先在独立临时资料目录中打开电脑已安装的 Google Chrome，并保持浏览器 sandbox 开启；它不会读取你的日常 Chrome Profile。
- 在这个由程序打开的独立登录窗口中完成 Bilibili 登录，程序会直接从本次受控浏览器上下文保存 Cookie；不要改用原先已经打开的普通 Chrome 窗口登录。
- 如果没有找到可用的 Chrome，页面会显示“正在下载 Playwright Chromium”。程序会联网下载浏览器到 `vendor\playwright-browsers\`，完成后自动打开登录页；下载大小和耗时以 Playwright 当前版本为准。
- 下载期间不要关闭 Launcher。失败时先检查网络、磁盘空间和安全软件，然后再次点击“登录”；已经完整下载的 Chromium 会被后续启动复用。
- 如果电脑只有 Edge，仍可用 Edge 访问 Web 控制台；账号登录窗口会按上述规则下载 Playwright Chromium。
- 不要向他人提供 Cookie，也不要把 Cookie 粘贴到聊天、截图或问题报告中。
- 如果测试人员不了解 Cookie 的含义，应继续匿名测试，不要手动提取登录态。

## 10. 大模型配置（可选）

不配置大模型时，系统仍可使用本地规则进行高光评分。大模型用于辅助复核、文案和网感资料库，可能产生服务商费用。

需要测试时：

1. 打开 Web 控制台的“模型”页面。
2. 点击“新增模型”。
3. 填写服务商名称、`base_url`、模型名和 API Key。
4. 保持“启用”，设置优先级。
5. 可先直接点击“测试连通”；它使用当前表单且不会保存配置。页面会保留每个模型的成功响应或错误详情。服务只返回推理过程或空正文时会显示失败；DeepSeek 思考模式因此没有最终正文时，程序会关闭思考模式自动重试一次。
6. 确认连通后点击“保存全部”。

只有显示测试成功后再启用相关功能。不要把真实 API Key 写进公开文档或问题报告。

## 11. 场次时间线、审核和成品文件

只有分析流水线已启用并产生高光候选时，下面的页面才会出现数据：

1. 打开“场次时间线”，按主播、房间和录制场次找到本次直播；页面时间统一显示为 GMT+8。
2. 展开场次卡片查看高光节点。每个节点提供时刻、摘要、最多两条代表弹幕、置信度、来源信号以及动态/跨分段标记。
3. 点击节点中的“审片”播放候选；确认内容后点击“批准并出片”，不需要的候选点击“拒绝”。
4. 默认不显示已拒绝节点；需要核查时打开“包含已拒绝”。
5. 在“成品切片”中播放并检查成品。“发布（置 ready）”只会标记本地交付状态并导出清单，默认不会自动投稿。
6. 在“上传 / 设置”中点击“打开切片目录”。

需要重算时：

- 只改了审核阈值，点击场次卡片的“按新阈值重分析”。
- 改了房间手工词典、人工学习别名或 ASR 模型，点击“按新词典/模型重新转写”。
- 在“实时转写”编辑正文时，可额外输入若干行 `错误词=正确词`，勾选加入直播间词典；保存期间自动刷新不会覆盖未提交草稿。
- 重分析只替换可重建的自动结果；人工审核、手工边界、私有草稿、已生成成片、反馈和人工正文继续保留。

审片时请注意：

- “LLM 理由”来自音频峰值附近的候选分析窗口，播放器预览和最终成片必须完整覆盖该窗口；LLM 建议和静音吸附只能向外扩展边界。
- `start_offset` 表示视频开始点，`end_offset` 表示理由所述事件完整结束后的出点，不是事件开始时刻。
- 点击“拒绝”会同步停止该候选仍可取消的任务，并把所有未发布关联成片标记为拒绝；已经发布的成片保留真实状态。
- V0.1.17.1 不会自动重写旧版已有的人工审核或成片。若旧候选仍有错位，请保留原始录像并从对应场次执行安全重分析；不要删除数据库绕过保护。

常用目录：

```text
storage\raw\              原始录制分片
storage\clips\            成品切片
storage\ready_to_upload\  待上传清单
storage\blc.db             本地数据库
models\                    本地语音模型
```

建议小规模测试期间保持 Biliup 和“成品就绪后自动上传”关闭。

## 12. 正常停止和再次启动

### 正常停止

1. 先在 Web 页面停止所有正在录制的房间。
2. 等待“录制状态”不再显示活动会话。
3. 回到黑色 Launcher 窗口，按 `Ctrl+C`。
4. 等待出现服务已停止的提示，再关闭窗口。

不要在写入录像、安装依赖或下载模型时直接关机。

### 再次启动

以后直接双击同一个 `BiliLiveCut-Portable.exe`。已安装的依赖和有效模型会被复用，不会每次重新下载。

## 13. 数据备份和升级

最重要的用户数据是：

```text
.env
storage\
```

备份前必须先停止录制并关闭 Launcher。把 `.env` 和整个 `storage` 文件夹复制到安全位置即可。

测试新版本时不要直接覆盖旧目录：

1. 把新版本解压到新的目录。
2. 先单独启动并完成基础自检。
3. 关闭新旧两个 Launcher。
4. 备份旧版 `.env` 和 `storage\`。
5. 再按新版本说明迁移数据。

不要复制旧版 `.venv`、`runtime` 或 `vendor\wheels` 到新版本，这些内容应由新 Launcher 管理。

## 14. 常见问题

### 双击后没有任何反应

- 确认不是在 ZIP 预览窗口中运行。
- 检查 Windows 安全中心或杀毒软件是否隔离了 EXE。
- 先核对 SHA-256；不要直接关闭杀毒软件。
- 将完整目录移动到 `D:\BiliLiveCut` 后重试。

### 浏览器没有自动打开

保持黑色窗口开启，手动访问 <http://127.0.0.1:8000>。

### 浏览器显示“无法访问此网站”

- 检查黑色窗口是否仍在运行。
- 等待模型准备和 `[6/6] Starting Web console` 完成。
- 如果提示端口 `8000` 被占用，先关闭之前启动的 BiliLiveCut 或占用该端口的程序，再重新启动。

### 账号登录提示正在下载 Chromium

这是电脑中没有可用 Google Chrome 时的正常回退流程，不是重复安装整个程序。保持网络和 Launcher 窗口开启；下载完成后登录窗口会自动出现。以后会复用 `vendor\playwright-browsers\` 中的浏览器文件。

### Full 安装出现 `wheelhouse is missing or empty`

说明完整包目录不完整。检查 `vendor\wheels\` 是否存在大量 `.whl` 文件。不要让 Launcher 在线回退；重新解压完整 Full ZIP。

### 大模型测试提示“未安装 openai”

`v0.1.17.1-Alpha` 最新构建已经把 OpenAI 兼容 SDK 纳入 Portable 运行时。先关闭服务并重新运行 Launcher，让依赖检查自动补装；如果仍出现该提示，说明当前 Full 的 `vendor\wheels\` 不完整或仍在使用旧构建，请重新下载并解压最新完整包。不要在 Portable 目录手工执行 `pip install -e`，也不要复用旧版 `.venv`。

### 控制台提示 pip 有新版本

最新构建已将 `pip 26.2` 连同 SHA-256 一起纳入 Portable 依赖锁。关闭服务并重新运行 Launcher 即会按锁自动升级；不要在 `.venv` 中手工执行 `pip install --upgrade pip`，否则本地版本会偏离发布时验证过的依赖集合。

### 出现 `THESE PACKAGES DO NOT MATCH THE HASHES`

新版 Full 应强制使用本地 wheelhouse，不应访问 PyPI 镜像。确认使用的是 `v0.1.17.1-Alpha` 最新 Full ZIP，并且没有只复制 EXE。不要修改锁文件或添加报错中的 sdist 哈希，直接重新下载并校验 Full ZIP。

### 模型下载很慢或中断

- 保持磁盘空间和网络稳定。
- 关闭 Launcher 后重新双击，已有有效文件会尽量复用。
- 如果分发者提供经过校验且版本完全匹配的 Engine Pack，可放到 EXE 同级目录后重试。

### 添加房间失败

- 确认勾选了录制授权。
- 使用完整直播间链接或正确房间号。
- 首次测试选择当前正在直播的房间。
- 原画不可用时，将 `.env` 中 `STREAM_QUALITY` 改为 `400` 或 `250` 并重启。

### 能录制但没有实时转写、候选或成片

先检查 `storage\raw\session_<数字>\` 是否已有 `.ts` 文件，再打开“功能开关”核对该直播间是否启用了“自动分析”；需要自动成片时还要启用“自动渲染”。原始录制成功与后续自动阶段是否启用是两件事。请在问题报告中注明开关状态，不要反复删除整个程序目录。

### 主播下播后任务一直显示重连

V0.1.17.1 Alpha 会先按 `LIVE_OFFLINE_CONFIRM_COUNT` 连续确认下播，再等待 `LIVE_SESSION_END_DELAY_S` 秒；期间恢复开播会取消停止。断流恢复受 20 次或 180 秒预算限制，单场还受 `RECORDING_MAX_DURATION_S` 限制。预算耗尽后当前会话会停止，并等待房间至少一次真实离线后才允许下一次开播自动录制；最后一个录制任务结束时网感定时采集会恢复。

### 候选理由、预览和视频内容不一致

先确认使用 V0.1.17.1 Alpha。新分析会用同一个候选窗口生成规则特征、LLM 理由和动态边界，并强制成片覆盖完整窗口。若旧结果仍错位，在“场次时间线”对对应场次执行重分析；改过词典或 ASR 时选择重新转写。受保护数据不会被静默改写。

### 拒绝候选后仍在成品列表显示 `reviewing`

V0.1.17.1 Alpha 的拒绝操作会同步未发布成片并默认从时间线过滤。升级前形成的不一致旧记录不会自动改库：若该候选在界面仍可操作，可以在新版重新拒绝；否则请先备份 `storage/blc.db`，再携带候选/成片 ID 反馈，不要直接修改数据库。已经发布的成片按设计保留，避免篡改真实外部发布结果。

### 修改 `.env` 后没有生效

`.env` 只在服务启动时读取。停止录制，按 `Ctrl+C` 关闭 Launcher，再重新启动。

### 想修复 Runtime

先备份 `.env` 和 `storage\`。在程序目录打开终端后执行：

```powershell
.\BiliLiveCut-Portable.exe --repair
```

不要删除 `storage\`，其中包含数据库和录制文件。

## 15. 给测试者的问题报告模板

报告问题时提供：

- Windows 版本，例如 Windows 11 23H2；
- 使用 Full 还是 Lite；
- 程序版本 `v0.1.17.1-Alpha`；
- 解压目录；
- 问题发生在 `[1/6]`～`[6/6]` 的哪一步；
- 黑色窗口最后 30 行文字或截图；
- Web“错误日志”中的报错；
- 是否存在 `storage\raw\session_<数字>\*.ts`；
- 是否使用 Engine Pack、Cookie、LLM 或代理。

提交前必须遮住 Cookie、API Key、密码、Webhook 和其他账号信息。不要上传 `.env`、数据库或完整用户目录。

## 16. 当前 Alpha 的测试边界

`v0.1.17.1-Alpha` 适合小规模、受控测试，不等同于稳定正式版。当前应重点验证：

- Full ZIP 下载、校验和解压；
- 首次离线依赖安装；
- 在线模型准备或 Engine Pack 安装；
- Web 控制台启动；
- 授权直播间添加、开始和停止录制；
- 主播下播或断流后的重试上限、自动收尾和页面状态清理；
- 场次时间线的主播、房间、GMT+8 时刻、摘要、代表弹幕与来源依据是否一致；
- 同一分段多个高光、跨分段高光及动态片头片尾是否覆盖完整事件；
- 房间词典、人工转写校正和场次重分析是否保留人工资产；
- 拒绝候选后未发布关联成片是否从成品列表消失；
- 原始文件完整性、磁盘占用和长时间运行稳定性；
- 不同 Windows 版本、杀毒软件和安装路径的兼容性。

自动分析、自动渲染、Cookie 登录、GPU 加速和自动上传应分开测试，不要在第一次试运行时同时开启所有功能。
