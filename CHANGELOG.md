# Changelog

## 未发布

## V0.1.17.1 Alpha (2026-08-06)

### 修复

- **release/ffmpeg-download**: Windows Release 构建不再单点依赖 Gyan FFmpeg 下载源；改用 BtbN 主源与 Gyan 备用源，按来源进行有限指数退避重试，并在安全提取前校验 ZIP 完整性；下载器在输出日志前主动切换 UTF-8，避免第三方临时 `503` 或 Windows `charmap` 代码页直接中断整个发布。
- **web/dirty-state**: 五秒轮询保留转写纠错与房间词典草稿、房间配置、功能开关、预约/主题选择及各类展开项；请求进行期间开始输入也不会被迟到响应覆盖，场次时间线“查看来源与评分”不再自动收起。
- **pipeline/review-render**: 非终结审核决策把任务移出 `awaiting_review`；人工批准独立成片会绕过自动渲染开关并进入正式渲染队列，网页后台渲染补齐任务租约与心跳，成功后同步成品 ID，避免恢复器抢回正在出片的任务。
- **analysis/multi-window-tail**: 五分钟分段除真实音频峰值外补充分散分析探针，并在活动直播中等待下一相邻分段转写后再分析，支持同段多候选、跨断点上下文和至少 30 秒后文。
- **recording/retry-terminal-state**: 连续取流失败默认时间上限调整为 180 秒；预算耗尽后停止本场并等待一次真实离线，防止平台仍报告直播中时无限创建重连会话；“停止并收尾”显示 `stopped`，仅显式暂停显示 `paused`。
- **transcription/cuda-task-concurrency**: 新增 `ASR_TASK_MAX_CONCURRENCY`（1～8）与控制台运行时设置；并行任务使用线程独立 ASR 流水线，允许 CUDA 在显存充足时并发处理多个录制分段。
- **version/release**: Python、C/Cython、Rust、Portable、Engine Pack、Docker、测试与用户文档统一升级为 `0.1.17.1-alpha`，Engine Pack 兼容区间调整为 `0.1.17.1-alpha ≤ app < 0.1.18`。

## V0.1.17 Alpha (2026-08-05)

### 变更

- **web/session-timeline**: 将控制台的逐候选平铺列表升级为按主播和录制场次归组的 GMT+8 时间线；节点展示高光时刻、摘要、1～2 条代表弹幕、置信度、来源信号、动态边界、跨分段状态和审核入口，默认隐藏已拒绝节点。
- **analysis/multi-highlight**: 每个五分钟原始分段最多分析 4 个相互独立的音频峰值，并支持跨相邻分段提取上下文与成片；入点、出点改为动态最小前后文加 LLM/静音向外扩展，不再固定为 1 分 30 秒。
- **analysis/danmaku-alignment**: 弹幕信号默认按 `7.5` 秒接收延迟向前对齐画面，仅调整评分窗口，保留原始采集时间；时间线同时提取 1～2 条高频代表弹幕。
- **analysis/diversity**: 同场临近候选按冷却距离与内容相似度去簇，避免一个爆点连续重复出片，同时保留相距足够远或语义不同的多个高光。
- **analysis/reanalysis**: 新增持久化场次重分析入口；阈值变化可直接重算，词典或 ASR 变化可重新转写后重算，且保留人工审核、手工边界、草稿、成片、反馈和人工转写等受保护资产。
- **transcription/room-dictionary**: 直播间支持手工热词与“错误词=正确词”学习别名；人工修正转写可选择回写房间词典，Fun-ASR-Nano 推理会消费合并后的有效热词。
- **analysis/feedback-learning**: 候选审核正负样本按候选原子回写阈值学习数据，并计算带负样本上限的分位数建议；审核日志补充直播间、场次、候选、操作者、决策和当时阈值。
- **recording/session-finalization**: 下播停止改为连续状态确认加可撤销的结束延迟，并增加单场最长时限；自然结束后执行最终重分析，网络错误仍按现有重试预算收尾。
- **version/release**: Python、C/Cython、Rust、Portable、Engine Pack、Docker、测试与用户文档统一升级为 `0.1.17-alpha`，Engine Pack 兼容区间调整为 `0.1.17-alpha ≤ app < 0.1.18`。

### 修复

- **security/codeql**: 候选去重指纹由 SHA-1 升级为 SHA-256；转写纠错词边界清理改为线性扫描，避免超长用户输入触发高代价正则回溯。
- **analysis/timezone**: 统一会话、分段、弹幕和候选的时区归一化，修复混合 naive/aware `datetime` 在时间线、跨分段窗口和弹幕统计中的比较错误。
- **transcription/repetition**: Fun-ASR-Nano 局部复读先保守折叠超过合理次数的重复，整段退化才回退 Paraformer/Whisper；保留有语义的正常强调，并记录实际修复与回退元数据。
- **pipeline/cross-segment**: 跨分段候选保留相对当前分段的负偏移，原子提交同一分析任务产生的多个候选与事件，并在并发工作进程下安全抑制同场重复节点。
- **web/transcript-editor**: 自动轮询不再覆盖正在编辑的转写草稿；保存人工正文时清除过期时间戳和旧整理结果，并可立即触发场次重分析。
- **portable/security**: 将 Python 3.11/3.12 Windows runtime lock 中的 `cryptography` 升级并固定为 `50.0.0`，修复 `GHSA-g6cj-pr64-35w5` / `CVE-2026-69247`，并重新生成完整依赖闭包与 wheel SHA-256。

## V0.1.16.5 Alpha (2026-08-03)

### 变更

- **analysis/llm-budget**: 五分钟转写整理与高光复核的默认最大输出预算统一提高到 `65536` token；配置校验范围分别为 `128-65536` 与 `512-65536`，实际消耗仍由服务商响应决定。
- **recording/reconnect**: 连续无法取流增加默认 20 次或 300 秒的双重停止上限，任一条件先满足即自动结束本场；成功产出新片段后预算归零，两个上限可分别用 `0` 禁用。
- **version/release**: Python、C/Cython、Rust、Portable、Engine Pack、Docker、测试与用户文档统一升级为 `0.1.16.5-alpha`，Engine Pack 兼容区间调整为 `0.1.16.5-alpha ≤ app < 0.1.17`。

### 修复

- **review/rejection-state**: 候选页与审片工作台的拒绝操作统一在一个事务中同步候选、审核事件、仍可取消的任务和所有未发布关联成片；成品列表过滤拒绝记录，审核撤销可恢复新版快照中的关联状态，已发布结果保持不变。
- **analysis/transcript-window**: 同步评分与流水线任务统一从音频峰值候选窗口提取转写、关键词、语速、弹幕和趋势输入；词级时间戳可用时精确裁剪，旧数据按时长比例裁剪，避免五分钟分片后续内容混入候选理由。
- **analysis/highlight-boundary**: 明确 LLM `start_offset` / `end_offset` 相对候选窗口且分别表示视频入点与完整出点，再换算到原始分片；成片边界必须覆盖完整分析窗口，LLM 建议和静音吸附只能向外扩展，避免理由所述事件发生在视频结尾之后。
- **publishing/transcript-window**: 审片正文和投稿文案按最终保存边界重新汇总跨分片转写，以最终成片内容作为文案唯一事实来源，不再把更宽评分窗口或成片之后的文本写入草稿。
- **recording/lifecycle**: 录制自然结束或异常退出后清理管理器任务、房间运行标记并在最后一个录制结束时恢复网感采集，修复主播下播后页面长期保持运行状态。
- **transcription/quality**: 长转写中的局部 ASR 解码循环也会触发 Paraformer、Whisper 回退；LLM 整理提示会保守清除残余的 ASR/VAD 边界复读，同时保留主播有语义的强调、复述和口头禅。
- **docs/release**: README、Portable README、简体中文 User Guide、Docker 说明、第三方模型声明与 Changelog 同步当前默认值、审核状态、时间轴语义、故障排查和升级边界。

## V0.1.16.4 Alpha (2026-08-02)

### 变更

- **transcription/long-audio**: 录制产生的 TS 在进入 ASR 前统一解码为 16 kHz 单声道 PCM WAV；Fun-ASR-Nano 复用 Engine Pack 内的 FSMN-VAD，并以可配置的 30 秒上限切分五分钟音频，推理启用显式缓存、中文语言与句级时间戳。
- **web/transcription**: 实时转写条目新增“重新识别”；可原子删除旧转写及尚未人工审核、尚未渲染的自动分析结果并重新入队，活动任务、人工审核、确认主题或成片资产会返回冲突而不覆盖数据。
- **analysis/highlight**: 默认初筛/候选/审核阈值调整为 `0.35/0.45/0.40`，自动批准/发布阈值调整为 `0.72/0.80`；爆点默认保留 60 秒前文，可跨越前一个原始分段合并渲染。
- **web/source-identity**: 直播间、录制会话、转写、弹幕、候选、审片队列和任务队列统一显示主播名与房间号。

### 修复

- **transcription/quality**: 新增空输出与连续重复退化检测。Fun-ASR-Nano 异常输出会依次回退 Paraformer、Whisper；最终结果仍不可用时任务失败，污染文本不会落库、调用 LLM 整理或进入高光分析。
- **logging/asr**: 修正受影响的 Loguru 参数占位符，模型路径与加载信息现在会正确写入日志。
- **pipeline/source-isolation**: 任务、原始分段、候选与审核事件增加同会话一致性校验，事件持久化真实 `segment_id`，防止多直播间后续流程串联。
- **review/preview**: 审片工作台可直接打开尚未出片的候选；播放器与波形共用按边界指纹缓存的按需预览，并按候选覆盖范围合并多分段转写。

## V0.1.16.3 Alpha (2026-08-01)

### 变更

- **recording/segments**: 默认分片目标由 60 秒调整为 300 秒；继续由 FFmpeg 在关键帧边界完成切分，因此实际单片时长允许小幅浮动，并保留环境变量覆盖。
- **transcription/asr**: 默认主引擎调整为 Fun-ASR-Nano；无有效输出时依次回退 Paraformer 与 Whisper，并记录实际引擎、主引擎状态和回退原因。
- **transcription/llm**: 每个切片完成 ASR 与房间别名纠错后，可调用已配置的 OpenAI 兼容 LLM 补全标点、整理可读正文并生成不超过 120 字的片段概括；分析消费整理正文，原始 ASR 继续保存在现有字段中，失败时安全降级且不改变数据库 Schema。
- **web/features**: 功能开关页新增“单切片 LLM 转写整理”运行时开关；实时转写页展示整理正文、片段概括、原始 ASR 与实际语音引擎。
- **trends/collector**: 单次趋势采集最多请求 12 条，解析器可保留被截断数组中此前完整闭合的对象，降低长响应整批丢弃风险。
- **version/release**: Python、C/Cython、Rust、Portable、Docker、测试与文档统一升级为 `0.1.16.3-alpha`，Engine Pack 兼容区间调整为 `0.1.16.3-alpha ≤ app < 0.1.17`。

- **portable/dependencies**: 将 `pip 26.2` 纳入 Python 3.11/3.12 严格哈希运行时锁，Launcher 使用 `pip freeze --all` 校验并自动升级旧 `.venv`；同步升级 FastAPI、Uvicorn、SQLModel、Pydantic、websockets、aiofiles、faster-whisper、FunASR、ModelScope 及其兼容传递依赖。
- **build/toolchain**: 构建工具升级至 `wheel 0.47.0` 与 `Cython 3.2.9`；固定源码 bootstrap wheel 已重新构建并确认 SHA-256 保持不变，慢速下载的读取超时提高至 300 秒。

### 修复

- **pipeline/transcription**: 移除转写计算结果中无消费者的 `text_version` 陈旧字段，避免 Paraformer 与 SenseVoice 已完成后因读取不存在的 `Settings.transcript_version` 而将任务错误标记为失败；补充真实 compute 成功路径回归测试。
- **transcription/portable**: Paraformer 本地模型兼容仅含 ModelScope `configuration.json` 的 CAM++ v1.0.0 Engine Pack，显式注册 `CAMPPlus` 架构、`WavFrontend` 与已验证的本地权重；含 `config.yaml` 的新版模型继续使用 FunASR 原生加载，避免模型目录被误作 registry key 而导致实时转写失败。
- **analysis/danmaku**: 弹幕基线按 SQLModel 实际返回的 datetime 标量处理，修复将时间当作单元素元组解包导致分析任务崩溃。
- **pipeline/state**: 状态机允许无候选的分析任务从 `analyzing` 直接进入 `completed`，测试改为调用生产状态机而不是维护一份重复矩阵。
- **analysis/llm**: 高光复核输出在理由字段被截断时可恢复已经完整生成的布尔判断和评分，评分会限制到 0–1；无法恢复时同时记录输出首尾，便于定位服务端截断。
- **web/rooms**: 直播间录制选项和直播间独立功能开关存在未保存草稿时，暂停对应表单重绘并显示提示；开始、停止或恢复录制仍会立即同步生命周期状态、活动会话、操作按钮和录制中锁定项，保存最后一组草稿后恢复完整刷新。
- **portable/release**: 同步版本真源、Payload、模型锁与 Fixture Engine Pack 的固定源码身份，补充跨文件一致性回归测试，避免 Release 在 Payload 合约校验阶段因源码 SHA 漂移而中断。

## V0.1.16.2 Alpha (2026-07-31)

### 变更

- **bilibili/danmaku**: 按直播网页重写弹幕链路：读取公开 WBI 图片键并签名 `getDanmuInfo`；有 Cookie 时优先登录访问，业务错误或鉴权拒绝后立即以 `uid=0` 匿名兜底，并在匿名连接存活期间定时恢复登录。新增可配置的单场登录失败上限与重试间隔，达到上限后本场固定匿名；同时校验鉴权回复、切换候选 WSS 节点，并将 `-352` 明确识别为平台风控而非简单判定 Cookie 过期。
- **recording/transcription**: 新增 `RECORDING_PIPELINE_ENABLED` 配置真源和控制台“录制实时转写”全局开关；Web 手动录制、预约、恢复及 CLI 默认读取该值，CLI 支持 `--pipeline/--no-pipeline` 单次覆盖，并在录制状态中显示本次会话是否启用实时转写。
- **plugins/highlight**: 插件 API v1 新增唯一 `highlight_scorer` 能力提供者、无 ORM 评分 DTO、`off/shadow/champion` 模式、房间级覆盖、插件模块清理和规则评分回退。
- **pipeline/highlight**: 分析 Worker 复用已解码音频并向插件提供转写、弹幕基线/窗口和 ASR 快照；Champion 概率可替换规则主评分，Shadow 仅记录观测，预测身份和错误进入候选元数据与结构化日志。
- **review/feedback**: 人工审核事务提交后把稳定样本 ID、明确正负标签、Schema 身份和预测时特征快照回传给原评分插件；撤销与非内容质量决策会删除旧标签，插件故障不回滚人工审核。
- **docs/tests**: 插件接口文档补充高光评分与反馈契约，并增加清单校验、单提供者、宿主数据适配、审核映射、隔离回退和显式执行的真实外部插件加载测试；默认 CI 不再因缺少跨仓库源码产生跳过项。

### 修复

- **portable/llm**: 将 OpenAI 兼容 SDK 纳入 Python 3.11/3.12 的严格哈希 runtime lock 和启动导入体检；Full/Lite 首次安装及旧环境再次启动时会安装并验证 LLM 依赖，不再在 DeepSeek 等模型连通测试中提示缺少 `openai`。

## V0.1.16.1 Alpha (2026-07-28)

### 变更

- **clipping/review**: 审片边界调整改为严格 JSON 请求和录像覆盖校验；重渲染显式使用已保存边界并生成独立版本文件，不再临时改写候选记录或覆盖既有成品。
- **recording**: 增加优雅/强制停止、持久化人工暂停、录制生命周期查询和直播高光打点；暂停中的房间不会被自动恢复，打点窗口会在会话结束时按真实媒体范围收敛。
- **review/security**: 增加多人审核队列、审核员独立账号、领取租约与管理员强制接管、盲审、私有草稿、单步撤销和结构化审计；审核员仅能访问审核页面及其所需媒体。
- **web/jobs**: 增加持久化后台作业管理器和 `/api/jobs` 查询、取消、重试接口；候选出片、审片重渲染、合集渲染及上传操作改为立即返回作业，支持去重、进度、错误和重启恢复。
- **render**: 主剪辑、派生版本和合集 FFmpeg 改为可协作取消的子进程执行；取消时主动终止当前命令并清理未完成输出。
- **upload**: 已开始的上传禁止不安全取消；服务中断后标记为需核对结果而不自动重放，降低重复投稿风险。
- **plugins/web**: 增加本地插件目录安全发现、显式启停、生命周期与命名空间设置接口；控制台新增插件中心和每个插件的独立设置页，根目录 `plugin/` 提供清单 Schema、接口文档和示例。
- **ui**: 控制台升级为分组侧边导航和统一响应式工作台，补齐窄屏布局、焦点状态、任务作业入口、独立审核队列与插件设置交互。
- **version/release**: Python、C/Cython、Rust、Portable、Docker、GitHub Actions、测试和文档统一升级为 `0.1.16.1-alpha`，Engine Pack 兼容区间调整为 `0.1.16.1-alpha ≤ app < 0.1.17`。
- **docs**: README 按补丁版本完整汇总 V0.1.15 变更，并新增独立 V0.1.16 功能章节。

### 修复

- **recording/ui**: 服务门面只从通知模块导出通知 API，避免旧版仪表盘实现覆盖正式服务并读取另一套录制管理器；开始录制后直播间页面会显示真实生命周期状态和活动会话。
- **features/ui**: 控制台新增独立“功能开关”页，按直播间集中暴露自动录制、分析、渲染、审核和上传五项流水线开关，以及预约、阈值学习、弹幕情绪和审核阈值；房间更新 API 同步接收全部字段。
- **models/ui**: 模型配置表单增加未保存状态保护；新增、编辑、切换或删除模型后，五秒轮询不再覆盖本地草稿；“测试连通”直接使用当前表单且不落盘，并在页面保留每个模型的响应或错误详情。
- **tasks/web**: 任务列表接口按属性读取 Worker 统计，修复将统计字典当作函数调用导致 `/api/tasks` 返回 500。
- **pipeline/startup**: 孤儿任务恢复按标量读取已有片段 ID，修复数据库已有任务时将整数当作 ORM 对象访问而导致 Portable Web 控制台启动失败。
- **portable/full**: Payload 文件匹配不再把 `.env.example` 误判为敏感 `.env`，Full 初次运行会从模板生成根目录 `.env`；模板缺失时启动器会明确报错而不再静默跳过。

## V0.1.15.3 Alpha (2026-07-27)

### 修复

- **release**: Lite 首次安装 smoke 驱动在入口将 stdout/stderr 切换为 UTF-8，修复 Windows runner 使用 `cp1252` 回显中文 Launcher 日志时触发 `UnicodeEncodeError`。
- **release**: Lite Doctor 冒烟在验证预期失败摘要后显式返回成功，避免 PowerShell 保留原生命令的预期非零退出码而误判整个 Release step 失败。
- **release**: Release 标签校验与 GitHub prerelease 判定统一使用小写规范化版本，兼容已有的 `-Alpha` 标签并避免被误判为正式版。
- **ci/package**: CI 与 Release 在直接执行 `setup.py build_ext` 前显式安装 `setuptools>=77` 和固定版 Cython，修复 Windows Python 3.11 runner 使用旧构建后端时拒绝 SPDX `license = "MIT"` 的问题。
- **portable/release**: Engine Pack 模型锁摘要统一按 LF 规范化换行后计算，消除同一 JSON 在 Windows CRLF 与 GitHub Actions LF checkout 下产生不同 SHA-256 的跨平台失败。
- **license/release**: 项目代码正式采用 MIT License（Copyright (c) 2026 StarGazerQQD），并将许可证纳入 Python 包、Payload、Portable Lite/Full、GitHub Release 与发布完整性门禁。
- **release**: sdist 明确收录前端 ES Module 交互检查脚本，消除源码包与版本控制文件集合不一致。
- **release**: Full 跨制品校验仅匹配发行根目录直属许可证，避免将 Portable Python 随附的第三方 `LICENSE.txt` 误判为重复项目许可证。
- **release**: Engine Pack 内嵌元数据现在必须与当前模型锁 SHA-256 完全一致，避免版本升级后继续携带旧锁摘要。
- **docs**: 修正 Portable Lite 构建命令示例，并增加禁止文档重新引入过期命令的回归测试。

## V0.1.15.2 Alpha (2026-07-22)

### 修复

- **portable**: Launcher、Engine Pack 构建/下载、Lite/Full、Payload 与旧版 Bundle 等全部 Portable CLI 入口统一将 stdout/stderr 切换为 UTF-8，并为不可编码字符保留回退表示，修复 Windows `cp1252` 控制台输出中文时直接崩溃或误报构建失败。
- **portable/native**: Windows Payload 改为在 Windows runner 构建，并以当前 Python ABI 的实际 `.pyd` 文件作为成功条件；禁止将 Linux `.so` 或旧 ABI 模块装入 Windows Portable，Full 离线冒烟会验证 C、Cython 与 Rust 后端均已加载。
- **native**: Cython 第二轮加速的时间戳和长度/索引统一使用双精度与 `Py_ssize_t`，修复 Unix epoch 分桶及长时间轴 SRT 与 Python fallback 不一致；Rust 构建改为实时显示 Cargo 输出。
- **subtitle**: `line_gap_ms` 现在按词间停顿阈值执行字幕断句，修复字幕模板配置已保存但不生效。
- **release**: Engine Pack CLI 在入口统一配置 UTF-8 输出并保留不可编码字符的回退表示，修复 Windows runner 使用 `cp1252` 代码页时 Fixture 构建因中文日志触发 `UnicodeEncodeError`。
- **login**: 系统 Chrome 与托管 Chromium 登录均显式启用 sandbox，并改为从独立 Playwright 上下文读取全部 Cookie 后按 Bilibili 域名边界筛选，修复新版 Chrome 下登录完成但无法捕获 Cookie 的问题。
- **web**: 修复候选片段拒绝请求的模板字符串未闭合导致前端 ES Module 初始化中断、页面按钮全部失效，并增加全量静态 JavaScript 语法回归检查。
- **portable**: Full 首次安装完成依赖后，`app.cli` 导入检查改为显式使用已安装的内容寻址 Runtime 源码，并在失败时保留原始 stdout/stderr。
- **portable**: 冻结 Launcher 的 Engine Pack、在线模型下载和模型校验入口改用绝对导入，修复 PyInstaller 顶层脚本缺少包上下文导致的模型准备崩溃。
- **release**: Full 离线 smoke 从实际 Payload 解压源码，在干净工作目录中使用 Full venv 导入 `app.cli`，并让冻结 EXE 使用 Fixture Engine Pack 完成模型准备。

- **portable**: PyInstaller 显式收集 Engine Pack Manifest 运行期依赖的 `model_catalog`、`version_loader` 及两份 JSON 配置，修复冻结 EXE 解压模型包后报 `ModuleNotFoundError`。
- **release**: Engine Pack 构建器同步生成 dist `engine-pack-info.json`，防止外部元数据残留旧版本；Full 构建清单补充 CRC32 并改为流式计算大文件哈希。
- **portable**: Engine Pack 内部 Manifest 统一使用安装器契约的 `format_version`，并让构建自校验调用真实 `load_manifest()`，避免“构建自检通过但首次安装失败”。

- **release**: Full 发布清单仅写入 `artifact_class=production` 的 Engine Pack CRC32，防止 CI 无模型构建误引用 Fixture 元数据。

- **cli**: 补齐 `python -m app.cli` 模块入口，修复 Portable Launcher 到达启动阶段后 Web 服务静默退出。

- **portable**: Launcher 显式调用 Typer `app.cli:app` 启动服务，不再依赖锁定 Payload 是否实现 `python -m app.cli`。

## V0.1.15.1 Alpha (2026-07-22)

### 变更

- **portable**: 账号登录优先调用系统已安装的 Google Chrome；不可用时复用或按需安装 Playwright Chromium，并补充相应状态提示与回归测试。
- **portable**: Full/Lite 运行时锁补齐 Playwright 依赖与安装导入冒烟检查，离线 wheelhouse 最低数量同步更新为 110。
- **docs**: 新增面向普通 Windows 用户的 Portable 从零使用说明，并同步下载、校验、浏览器和故障排查步骤。
- **repository**: 完善 Docker 构建上下文忽略项，修正 Docker 文档中的启动与停止脚本名称。

## V0.1.15 Alpha (2026-07-21)

### 变更

- **portable**: 固定源码基线升级至当前 `main` 的 `4bdaa13`，移除已被新基线原生吸收的历史 Backport，并增加 Payload 业务源码逐文件一致性回归测试
- **native**: PyO3/Rust 扩展构建显式使用当前虚拟环境的 Python，避免构建子进程找不到解释器后退回纯 Python 实现

## V0.1.14.12 Alpha (2026-07-21)

### 修复

- **release**: Full Bundle 的 `app.cli` 冒烟测试改用已完成离线依赖安装的 `$venvPython`，避免 runner 裸 Python 缺少 `typer` 导致发布误失败
- **release**: 增加工作流契约测试，禁止 `app.cli` 冒烟检查退回裸 `python`

## V0.1.14.11 Alpha (2026-07-17)

### 修复

- **database**: Schema 指纹去除 callable 内存地址并稳定排序约束，修复数据库重建后重启即被误判为不兼容
- **pipeline**: 修复转写提交写入不存在的 `SegmentTask.transcript_id` 并向 `enqueue_next()` 传入无效参数
- **cli**: `record --pipeline/--produce` 同步房间调度开关并传递房间主键，拒绝单独使用 `--produce`
- **asr**: 修复兼容入口从错误模块导入 `TranscriberBackend` 导致编排器无法加载
- **ci**: 覆盖率运行器同时校验 pytest 退出码，移除破坏默认配置测试的空 `WHISPER_MODEL`
- **release**: 正式构建禁止 fixture 绕过，修复标签门禁、Full ZIP/CLI smoke、产物聚合与校验和生成
- **version**: `__version_label__` 改为从版本真源动态生成，Docker 文档同步至当前版本
- **pipeline**: 修复 `acquire_resources()` 返回 bool 但被当作 dict 传递给 `release_resources(**cost)` 的 TypeError
- **models**: 删除空 `ENGINES_TO_DOWNLOAD=[]`，替换为 `_load_launcher_engines()` 加载统一 Catalog
- **models**: 子模型目录使用 `target_subdir` 而非完整 repository ID，防止目录名错误
- **asr**: 修复 `iic/Fun-ASR-Nano` → `FunAudioLLM/Fun-ASR-Nano-2512` 正确仓库 ID（backends.py 和 pipeline.py）
- **engine-pack**: Schema 升级至 v4，统一 Builder/Installer/Verifier 版本号
- **engine-pack**: 新增 `artifact_class` 字段区分 `production`/`fixture`
- **runtime**: 嵌入式 Payload identity 与 installed current.json 比对新旧 EXE
- **portable**: 删除 `requirements-bundle.txt` 及 wheels/mirror 死代码，只用 ABI 锁文件 + `--require-hashes`
- **full**: 删除 `continue-on-error`，wheels 从 lock 文件下载，零 wheel 立即失败
- **web**: 实现真实 CSRF 防护（Origin/Referer 校验、Basic Auth 后仍检查同源）
- **web**: Basic Auth 用户名和密码都必须校验
- **native**: 删除 `/arch:AVX2` 强制编译标志，使用 generic x86-64 基线
- **c**: 修复 `ac_build_failure()` 返回 void 但调用方未检测 `PyErr_Occurred()` 的 bug
- **release.yml**: 新增 production metadata 完整校验（嵌套哈希、commit、engine_ids、size）

## V0.1.14.8 Alpha (2026-07-15)

### 修复

- **builder**: 修复 `engine_pack_info.json` 缺失 `sha256`/`size_bytes`/`source_commit` 字段，导致 Lite EXE 构建校验失败
- **full.py**: 英文化所有运行时 print 语句避免 Windows CI cp1252 `UnicodeEncodeError`
- **release.yml**: 移除冗余 `certutil` checksums 步骤，拆分 Full 离线组件准备为独立步骤
- **README**: 新增 Engine Pack 本地生成说明，明确 GitHub Release 不包含 ASR 模型引擎包

## V0.1.14.7 Alpha (2026-07-09)

### Portable 发布工程系统性修复与版本统一

本轮为 Portable 发布工程系统性修复迭代，解决版本碎片化、模型定义不一致、校验缺失、Runtime 重用旧 Payload 等系统性问题。

**版本管理统一**
- 建立 `packaging/portable/config/version.json` 单一权威版本源
- 新增 `version_loader.py` 统一版本加载，所有模块统一引用
- 新增 `scripts/check_version_consistency.py` CI 检查脚本

**模型配置统一**
- 建立 `packaging/portable/config/model_sources.lock.json` 单一模型权威源
- 新增 `model_catalog.py` 统一模型加载与校验
- 修正 FunASR-Nano 仓库 (`iic/Fun-ASR-Nano` → `FunAudioLLM/Fun-ASR-Nano-2512`)
- 所有模型锁定 resolved_revision，确保可复现

**Engine Pack 完整性**
- 强制 SHA-256 + CRC32 双重校验
- `_safe_extract` 流式解压 + Zip Slip/Zip Bomb 防护
- 安装清单包含 schema version、zip SHA-256、source commit

**Portable EXE 构建**
- Lite EXE 禁止生成空 CRC32/SHA-256/模型信息的 EXE
- Full 包真正包含 Portable Python + Wheels + FFmpeg/FFprobe
- 内容寻址 Runtime Release ID，Payload SHA-256 变化自动触发重装
- Lite EXE 支持 `BLC_CI_BUILD=1` 环境变量跳过 Engine Pack 校验 (CI 构建用)

**Release 工作流增强**
- 新增 `build-sdist` job: 构建 sdist + wheel + Windows 源码 ZIP + SHA256SUMS
- 新增 `build-payload` job: 从固定 commit `731a31c` 提取源码并打包 Payload
- 新增 `build-windows-lite` job (Windows runner): PyInstaller 编译 Lite EXE
- Release 资产包含: sdist、wheel、源码 ZIP、Lite EXE、SHA256SUMS
- 注: Engine Pack ZIP 因模型体积过大 (10GB+) 由本地手动构建上传

**Launcher CLI 升级**
- `argparse` 替代手动 `sys.argv` 解析
- 新增 `--doctor`、`--verify-models`、`--repair`、`--version`、`--offline`、`--fallback-online`

**Cython 兼容性**
- 修复 `_speedups_round2.pyx` 中 Cython 3.2.8 不兼容的 `PyList_GET_ITEM` 调用

**CI 发现的鲁棒性修复**
- 修复 `tests/test_version_consistency.py` F401: 删除未使用的 `import pytest`
- 修复 `tests/test_model_catalog.py` F401: 删除未使用的 `import pytest`
- 修复 `tests/test_version_consistency.py` E741: 重命名模糊变量 `l` → `line_text`
- 修复 `tests/test_version_consistency.py` F541: f-string 无占位符改为普通字符串
- Ruff format: 两个测试文件重新格式化
- 删除 v0.1.14.6 重构临时快照 `tests-after-v0146.txt` / `tests-before-v0146.txt`
- `.gitignore` 新增 `/tests-*.txt` 规则防止临时测试快照入库

**测试**
- 新增 `test_version_consistency.py` 版本一致性测试
- 新增 `test_model_catalog.py` 模型目录完整性测试

## V0.1.14.6 Alpha (2026-07-08)

### 发行结构重构 — Docker/Rust/Portable 目录迁移与四引擎 Engine Pack

本轮为发行结构重构，将 Docker 发行文件迁移至 `packaging/docker/`，Rust 构建脚本迁移至 `tools/native/`，
Portable 代码重构为 `src/blc_portable/` 模块化结构，并构建独立的四引擎 ASR Engine Pack。

**目录迁移**
- `Dockerfile` + `docker-compose.yml` → `packaging/docker/`，同步更新所有引用和 Compose 路径
- `build_rust.py` → `tools/native/`，同步更新所有脚本、文档和 CI 引用

**Portable 结构重构**
- 可导入代码迁移至 `packaging/portable/src/blc_portable/`，模块化拆分 launcher/payload/engine_pack/builders/util
- 根构建脚本保持为薄入口，正式逻辑全部在 `src/blc_portable/` 中
- 避免创建 `packaging/__init__.py`，防止遮蔽第三方 `packaging` 库

**四引擎 ASR Engine Pack**
- 独立构建包含 Paraformer/SenseVoice/FunASR-Nano/Whisper 四个引擎完整模型的 ZIP
- 支持分卷 (1.8 GiB/卷) 以适应 GitHub Release 单文件限制
- Engine Pack 与 Lite EXE / Full ZIP 完全分离，不嵌入不捆绑
- Launcher 内嵌 Engine Pack CRC32/SHA-256/版本信息，启动时自动校验
- 运行时分五种路径准备模型：已安装 → 本地完整 ZIP → 本地分卷 → GitHub Release → 官方源全量下载
- 模型安装至 `<程序根目录>/models/`，独立于源码 Release 目录
- 原子安装、安全解压、Zip Slip 防护

**测试与 CI**
- 全量 pytest 通过
- Ruff check + format check 通过
- 测试 Node ID 完整对比无减少
- CI portable-test 新增 Engine Pack 测试

## V0.1.14.5 Alpha (2026-07-07)

### Portable 内嵌 Payload 构建系统 — 源码基线固定、离线发行

本轮为架构迭代，建立源码从固定 Git Commit 提取、内嵌到 Portable EXE 的完整发行链路。

**目录迁移**
- `Publish-PnP/` → `packaging/portable/`，同步更新所有引用和 `.gitignore`

**Payload 构建系统**
- `payload_manifest.py`: 定义 Payload Manifest 规范 (format_version 1)，含逐文件 SHA-256
- `source_snapshot.py`: 从 `74c21b4` 通过 `git archive` 安全提取源码，禁止工作区污染
- `build_payload.py`: 构建 `source_payload.zip`，自动验证可复现性（连续构建 SHA-256 一致）
- `runtime_layout.py`: Runtime 目录布局、`staging` → `rename` 原子安装、`current.json` 原子更新

**Portable Launcher**
- `launcher.py`: 重写为从 EXE 内置 Payload 释放源码，首次启动 GitHub 请求数为 0
- `build_exe.py`: Lite 版构建 (PyInstaller one-file)
- `build_full_bundle.py`: Full 离线包构建
- `portable_launcher.spec`: PyInstaller 规格文件

**Payload 数据**
- Payload ZIP: 187 文件，426 KB
- SHA-256: `93ff7bfab0cba6c1e88f3d9a815b21164aa70a3b0110be70adfe15cf84f92708`
- Source: `74c21b4` (`74c21b401f1da4ef52f0333c94e3874e80f8ceef`)
- Release Overlay: `app/__init__.py`, `pyproject.toml`, `README.md`, `CHANGELOG.md`, `setup.py`, `setup_c.py`

**测试 (19 项全部通过)**
- Source Snapshot: Commit 解析、提取、Overlay 受控
- Payload: ZIP 构建、Manifest 校验、Zip Slip 防护、可复现性
- Runtime: 原子安装、staging 清理、current.json、重复安装跳过
- 用户数据: `.env` 不覆盖、Release 目录不含敏感文件
- 安全: Manifest 篡改检测、Payload 篡改检测

## V0.1.14.4 Alpha (2026-07-07)

### 稳定性收口 — 全链路崩溃安全

本轮为质量迭代，焦点是"远端结果不丢失"和"进程崩溃后状态可恢复"。

**Phase 4：上传崩溃窗口与 reconciliation**
- 新增 `RemoteUploadResult` 与 `classify_upload_error` — 安全异常分类：无法证明请求未到达平台时标为 `remote_result_unknown`，禁止自动重试
- 新增持久化日志 `app/publishing/journal.py` — DB 不可用时将远程成功写入 JSONL
- 新增 `app/pipeline/publish_recovery.py` — 重启后从 Journal 回填远程成功到 DB

**Phase 5：stale recovery 与恢复器**
- `recover_stale_upload_attempts` — 超时 `IN_PROGRESS` Attempt → `RECONCILIATION_REQUIRED`
- `sync_segment_task_from_attempt` — Attempt 状态 → `SegmentTask` 同步
- `full_recovery()` — 全量恢复统一入口

**Phase 7：故障注入与 Golden Path**
- 14 个单元测试：Journal 写入/回填/损坏恢复、stale attempt 恢复、异常分类 (DNS/拒绝连接/超时/断管/权限/兜底)
- 全量 pytest 304 通过

## V0.1.14.3 Alpha (2026-07-07)

### P0/P1 稳定性修复

- Phase 1: 删除 api.js placeholder, 审计 review.js
- Phase 2: 分析 compute 成为纯计算, _mark_scored 移至 commit
- Phase 3: 渲染 compute 使用 lease 专属临时文件
- Phase 4: 发布持久化 UploadAttempt, REMOTE_RESULT_UNKNOWN → RECONCILIATION_REQUIRED
- Phase 5: Transcript 错误处理 + 幂等路径修复, 删除冗余 heartbeat
- Phase 6: shutdown_event 替代跨模块 bool, 锁立即初始化
- Phase 7: 统一版本真源

## V0.1.14.2 Alpha (2026-07-07)

### CI 修复 + 全量代码规范审计

**CI Lint 修复**
- 修复 C4 拆分后 13 个 Pydantic 请求模型缺少 docstring (D101) 导致 `ruff check` 失败
- CI lint job 失败阻断了所有下游 test/audit/coverage-summary job
- 补全 `candidates.py`、`container.py`、`llm.py`、`rooms.py`、`schedules.py`、`topics.py`、`trends.py` 中所有 BaseModel 子类的 docstring

**全量代码格式化**
- `ruff format` 格式化 51 个 Python 文件，确保 CI format 检查通过
- `ruff check app/ tests/` 零错误通过

**版本升级**
- 版本号 `0.1.14.1-alpha` → `0.1.14.2-alpha`
- 同步 `app/__init__.py`、`pyproject.toml`、`setup.py`、`setup_c.py` 及 48 个模块文档字符串中的版本标签
- 全量 290/290 测试通过

---

## V0.1.14.1 Alpha (2026-07-07)

### 阶段 C2-C8 深层拆分 + 缓存清理

**根目录清理**
- 删除所有 `__pycache__`、`.pytest_cache`、`.ruff_cache`、`build/`、`bili_live_cut.egg-info/`、`storage/`、日志压缩包

**C2: transcribe.py 真正拆分**
- 提取 `transcription/models.py` — Word, EmotionEvent, ASRSegmentResult 等 DTO 类
- 提取 `transcription/backends.py` — TranscriberBackend, FunASRBackend, FasterWhisperBackend 及辅助函数
- 提取 `transcription/pipeline.py` — ASRPipeline, transcribe_segment, get_default_pipeline
- `transcribe.py` 保留为兼容门面, 全部公开导入路径有效

**C3: web/service.py 按业务实体拆分子文件**
- `web/services/` 下创建 rooms/candidates/clips/publishing/settings/dashboard/transcripts/schedules/trends/logs/learning/notifications 等 12 个子服务文件
- 各子文件从主 `service.py` 重导出对应函数, 原始 `service.py` 保持不变

**C4: web/routers/api.py 按资源拆分子路由器**
- `web/routers/` 下创建 rooms/candidates/clips/publishing/settings/dashboard/schedules 等子路由文件

**C5: clipper.py 拆分子模块**
- `app/clipping/` 下创建 models/ffmpeg_command/ffmpeg_probe/paths/validation 等子模块

**C6: cli.py 拆分子命令**
- `app/commands/` 下创建 record/serve/doctor/config/room 等子命令文件

**C7: db/models.py 按实体拆分子模型**
- `app/db/entities/` 下创建 room/recording/transcript/highlight/topic/clip/publishing/task/settings 等子模型文件

**C8: app.js 前端拆分**
- `web/static/js/` 下创建 api/common/dashboard/recording/review/clips/publishing/settings/monitor 等 JS 模块占位

**版本升级**
- 版本号 `0.1.14-alpha` → `0.1.14.1-alpha`
- 全量 290/290 测试通过
- Ruff 全部通过

---

## V0.1.14 Alpha (2026-07-07)

### 仓库清理、职责分层与可维护性重构

**阶段 A — 零风险仓库清理**
- 删除临时 CI 日志目录 (`temp_ci_logs/` 等) 和日志压缩包
- `.gitignore` 使用精确规则，避免误伤正式文件
- 确认 `.env` 未被 Git 跟踪

**阶段 A5 — CHANGELOG 归档**
- 主 `CHANGELOG.md` 只保留最近 3 个三级版本系列 (0.1.13/0.1.12/0.1.11)
- 更早版本归档到 `docs/changelog/CHANGELOG_PRE_0.1.X.md`
- 创建 `docs/changelog/CHANGELOG_INDEX.md` 导航全部归档

**阶段 D — 测试目录分层**
- `tests/` 按 `unit/` / `integration/` / `fault_injection/` / `golden/` 分类
- 测试收集数保持 290 不变
- `pyproject.toml` ruff 规则更新为 `tests/**/*.py`

**阶段 B — 加速模块归拢**
- C/Cython/Rust/Python fallback 统一归入 `app/accelerators/`
- `app.analysis.speedups` 保留为兼容门面
- 旧导入路径全部保持有效
- 更新 `setup.py`、`setup_c.py`、`build_rust.py` 的源路径
- Extension 模块名保持 `app.analysis._c_speedups` 不变

**阶段 C1 — 拆分 task_worker.py (1667行)**
- 提取 `app/pipeline/stage_result.py` — 状态转换矩阵、幂等键、任务标记
- 提取 `app/pipeline/workers/` — 各阶段 compute/commit/run 实现
- `task_worker.py` 保留 Worker 主循环、调度、并发管理
- 全部兼容重导出 (`_can_transition`, `_ensure_event`, `mark_active` 等)

**阶段 C2-C8 — 子包入口创建**
- `app/analysis/transcription/` — ASR 子系统模块化入口
- `app/web/services/` — Web 服务层模块化入口
- `app/commands/` — CLI 命令模块化入口
- `app/db/entities/` — 数据库模型模块化入口
- `app/web/static/js/` — 前端 JS 模块化入口

**版本升级**
- 版本号 `0.1.13.2-alpha` → `0.1.14-alpha`
- 全量 290/290 测试通过
- Ruff 全部通过

---

历史版本归档见 [docs/changelog/CHANGELOG_INDEX.md](docs/changelog/CHANGELOG_INDEX.md)。
