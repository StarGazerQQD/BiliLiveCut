# CHANGELOG — 0.1.16 系列

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

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
