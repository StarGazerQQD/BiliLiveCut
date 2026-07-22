# BiliLiveCut Portable 小白使用说明

适用版本：`v0.1.15.1-alpha` · 适用系统：Windows 10/11 x64

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

打开项目的 [GitHub Releases 页面](https://github.com/StarGazerQQD/BiliLiveCut/releases)，进入 `v0.1.15.1-alpha`，下载：

1. `BiliLiveCut-Portable-Full-0.1.15.1-alpha-x64.zip`
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
Get-FileHash ".\BiliLiveCut-Portable-Full-0.1.15.1-alpha-x64.zip" -Algorithm SHA256
```

4. 将输出的 `Hash` 与 `SHA256SUMS.txt` 中同名文件前面的值比较。英文字母大小写不同不影响结果。
5. 两者必须完全一致。若不一致，删除 ZIP 并从 Releases 页面重新下载；不要继续解压或运行。

## 4. 正确解压 Full ZIP

1. 新建目录，例如 `D:\BiliLiveCut`。
2. 右键 ZIP，选择“全部解压”。
3. 打开解压出来的 `BiliLiveCut-Portable-Full-0.1.15.1-alpha-x64` 文件夹。
4. 确认同一层能看到：

```text
BiliLiveCut-Portable.exe
portable-python\
vendor\wheels\
bin\ffmpeg.exe
bin\ffprobe.exe
README.txt
```

必须保留整个目录结构。不要只把 `BiliLiveCut-Portable.exe` 单独拖到桌面，也不要直接在 ZIP 预览窗口里双击运行。

## 5. 准备模型

Full 版包含运行环境，但不包含四个语音识别模型。

### 情况 A：没有 Engine Pack

这是普通测试者最常见的情况。保持网络连接，首次启动时 Launcher 会从 ModelScope 和 Hugging Face 下载全部模型。模型总量约 5.5 GB，可能需要较长时间。

### 情况 B：分发者提供了 Engine Pack

只接受与应用版本匹配的文件：

```text
BiliLiveCut-EnginePack-0.1.15.1-alpha.zip
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
COLLECT_DANMAKU=false
LLM_API_KEY=
TREND_ENABLED=false
UPLOADER=manual
```

说明：

- `ADMIN_PASSWORD` 留空：只允许本机通过 `127.0.0.1` 使用，首次测试最简单。
- `STORAGE_ROOT` 保持 `./storage`：数据库、原始录像和成品都在程序目录内，便于备份。
- `STREAM_QUALITY=10000`：请求原画；若匿名访问无法取得，可改为 `400` 或 `250`。
- `COLLECT_DANMAKU=false`：未登录时先关闭弹幕采集，减少无关报错。
- `LLM_API_KEY` 留空：使用本地规则评分，不产生 API 费用。
- `TREND_ENABLED=false`：不启用联网热点采集。
- `UPLOADER=manual`：只生成本地文件，不自动投稿。

### 7.3 不要先改这些配置

首次测试请保持 ASR 设备为默认 CPU，不要直接改为 CUDA：

```ini
ASR_PRIMARY_DEVICE=cpu
ASR_AUXILIARY_DEVICE=cpu
ASR_REVIEW_DEVICE=cpu
ASR_FALLBACK_DEVICE=cpu
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
```

先证明录制链路正常，再单独测试 GPU。错误的 CUDA 配置可能导致模型加载失败。

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
6. 在房间卡片中点击“开始录制”。
7. 打开“录制状态”，确认状态变为 `recording`，片段数量开始增加。
8. 先录制 2～5 分钟，再回到“直播间”点击“停止录制”。
9. 在程序目录检查：

```text
storage\raw\session_<数字>\
```

目录中出现 `.ts` 片段，表示从安装到录制的第一条链路已经成功。

> 当前 `v0.1.15.1-alpha` 的 Web 页面尚未完整暴露新的 `auto_analyze`、`auto_render` 等房间级自动化开关。页面“开始录制”可以验证原始录制，但不要把“实时转写、候选和成片是否立即出现”作为首次安装是否成功的唯一标准。

## 9. Cookie、弹幕和高清访问

Cookie 不是公开直播首次录制的必需项，但部分清晰度、弹幕或鉴权接口可能需要登录态。

- 首次测试建议保持 `COLLECT_DANMAKU=false`。
- 点击“账号管理”→“登录”后，程序会优先打开电脑已安装的 Google Chrome。
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
5. 点击“保存全部”，再点击“测试连通”。

只有显示测试成功后再启用相关功能。不要把真实 API Key 写进公开文档或问题报告。

## 11. 候选审核和成品文件

只有分析流水线已启用并产生高光候选时，下面的页面才会出现数据：

1. 在“候选审核”中点击“审片”查看候选。
2. 确认内容后点击“批准并出片”；不需要的候选点击“拒绝”。
3. 在“成品切片”中播放并检查成品。
4. “发布（置 ready）”只会把成品标记为可交付并导出本地清单；默认不会自动投稿。
5. 在“上传 / 设置”中点击“打开切片目录”。

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

### 出现 `THESE PACKAGES DO NOT MATCH THE HASHES`

新版 Full 应强制使用本地 wheelhouse，不应访问 PyPI 镜像。确认使用的是 `v0.1.15.1-alpha` 最新 Full ZIP，并且没有只复制 EXE。不要修改锁文件或添加报错中的 sdist 哈希，直接重新下载并校验 Full ZIP。

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

先检查 `storage\raw\session_<数字>\` 是否已有 `.ts` 文件。当前 Alpha 的 Web 自动化开关尚未完整暴露；原始录制成功与自动分析是否启用是两件事。请在问题报告中注明“原始录制成功，但无转写/候选”，不要反复删除整个程序目录。

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
- 程序版本 `v0.1.15.1-alpha`；
- 解压目录；
- 问题发生在 `[1/6]`～`[6/6]` 的哪一步；
- 黑色窗口最后 30 行文字或截图；
- Web“错误日志”中的报错；
- 是否存在 `storage\raw\session_<数字>\*.ts`；
- 是否使用 Engine Pack、Cookie、LLM 或代理。

提交前必须遮住 Cookie、API Key、密码、Webhook 和其他账号信息。不要上传 `.env`、数据库或完整用户目录。

## 16. 当前 Alpha 的测试边界

`v0.1.15.1-alpha` 适合小规模、受控测试，不等同于稳定正式版。当前应重点验证：

- Full ZIP 下载、校验和解压；
- 首次离线依赖安装；
- 在线模型准备或 Engine Pack 安装；
- Web 控制台启动；
- 授权直播间添加、开始和停止录制；
- 原始文件完整性、磁盘占用和长时间运行稳定性；
- 不同 Windows 版本、杀毒软件和安装路径的兼容性。

自动分析、自动渲染、Cookie 登录、GPU 加速和自动上传应分开测试，不要在第一次试运行时同时开启所有功能。
