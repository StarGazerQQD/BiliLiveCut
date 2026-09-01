# CHANGELOG — 0.1.17 系列

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

## V0.1.17.4 Alpha (2026-08-20)

### 修复

- **web/session-history**: “实时转写”和“弹幕热度”改为按完整录制场次历史选择，不再用固定最近条数把旧记录挤出可见范围；两个页面分别记住当前场次，内容没有变化时跳过列表重绘。
- **web/transcript-session-draft**: 转写纠错编辑器展开或存在未保存内容时锁定场次切换并暂停轮询重绘，避免切换下拉或五秒刷新丢失正文、词典别名和勾选状态。
- **recording/room-metadata-refresh**: 每次手动、预约或自动流程真正开始新录制前重新查询直播间标题和主播名；详情查询失败时保留最近一次成功资料且不阻断已授权录制。

### 变更

- **version/release**: Python、C/Cython、Rust、Portable、Engine Pack、Docker、工作流、测试与用户文档统一升级为 `0.1.17.4-alpha`；GitHub Release 标签固定为 `v0.1.17.4-Alpha`。

## V0.1.17.3 Alpha (2026-08-12)

### 修复

- **asr/whisper-compute**: Whisper 兜底遇到当前 CPU/CUDA 后端不支持的显式 CTranslate2 计算类型时改用 `auto` 重试；其他模型和设备错误仍原样抛出，修复主 ASR 回退时偶发的 `Requested int16 compute type`。
- **web/draft-retention**: 转写纠错与房间词典在展开编辑器时即暂停列表重绘，并覆盖输入、选择框和勾选框事件；直播间配置、功能开关、网感定时设置、上传开关、LLM、插件设置、审片备注及合集章节草稿统一增加迟到响应与保存竞态保护，避免五秒轮询或保存期间继续输入时覆盖未保存内容。
- **web/transcript-source**: 实时转写卡片显示每条 ASR 对应的源 TS 文件名，提供复制入口与无损 MP4 导出；导出以 bitstream filter 校正视频 PTS/DTS 并缓存结果，在完整保留视频帧、AAC 音频包和原始编码的同时，避免常规 TS 重封装后第一帧因视频起点晚于音频而显示黑画面；历史缺失片段会显示明确的不可用状态。
- **clipping/first-frame-timestamp**: 主成片、审片预览和派生版本在重编码时统一把首个有效音视频帧时间轴归零，避免从 TS 起点或分段边界出片时生成首帧黑色空窗，同时保持后续音画同步。
- **version/release**: Python、C/Cython、Rust、Portable、Engine Pack、Docker、测试与用户文档统一升级为 `0.1.17.3-alpha`；Alpha 发行移除旧数据库字段、旧导入门面、旧配置键与旧 Runtime/Payload/Engine Pack 清单兼容，只接受当前精确版本和当前 Schema。
- **release/contracts**: 删除 Payload 构建期版本覆盖文件与 `release_overlays` 字段，Payload Schema 升至 7；Release 标签改为逐字符匹配当前版本真源，不再接受历史大小写写法。
- **release/tag-contract**: 明确区分内部 PEP 版本 `0.1.17.3-alpha` 与唯一的 GitHub Release 标签 `v0.1.17.3-Alpha`；标签由版本真源显式映射并逐字符校验，不再因文档规定的外部标签大小写而阻断发布。
- **release/payload-contract**: Release 的 Payload Manifest 校验改为读取版本配置中的 `payload_schema` 真源，避免 Schema 升级后工作流仍使用旧硬编码版本而阻断发布。
- **release/artifact-identity**: Release 跨制品身份校验器及测试夹具改为只读取 Payload Schema 7 的当前身份字段，不再要求已删除的 `release_version`、`source_commit` 与 `source_commit_short`，避免 Smoke tests 在有效 Payload 上误判失败。
- **portable/fresh-install**: Lite 冻结启动器改为从 PyInstaller 资源目录读取内嵌项目 MIT License，并在生成应用 `.env` 时剔除仅供 Launcher 安装依赖使用的 pip 镜像项，修复空目录首次安装提前退出的问题。

### 变更

- **analysis/whole-session-summary**: 录制结束并完成最终跨分片分析后，按 GMT+8 顺序汇集同场全部最终 ASR，并将完整上下文一次性交给 LLM 做全局分析；不再拼接高光节点或分段摘要。总结请求、重启恢复和 ASR 签名均持久化，人工纠错或重分析会使旧总结失效，LLM 无有效结果时保留失败状态并按任务策略重试。

## V0.1.17.2 Alpha (2026-08-11)

### 修复

- **web/timeline-scroll-retention**: 场次时间线轮询会跳过内容未变化的会话列表与展开详情，不再每五秒销毁并重建正在阅读的 DOM；确有新节点或状态变化时会在刷新完成后恢复原滚动坐标，避免查看摘要或来源评分时自动跳回页面顶部。
- **version/release**: Python、C/Cython、Rust、Portable、Engine Pack、Docker、测试与用户文档统一升级为 `0.1.17.2-alpha`，Engine Pack 兼容区间调整为 `0.1.17.2-alpha ≤ app < 0.1.18`。

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
