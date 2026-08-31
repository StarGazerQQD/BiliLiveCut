# Event-first 热点检测器

`HotspotDetector` 在创建视频候选之前回答“这一时段是否发生了值得关注的变化”。它不调用 LLM，也不把 ASR 当作前置条件；检测结果先以 `HotspotEvent(status=provisional, candidate_id=NULL)` 保存。后续事件补全与成片评分再决定是否进入既有 `HighlightCandidate` / `HighlightEvent` 链。

## 时间尺度

默认按 10 秒生成信号桶，用此前最多 90 秒作为直播自身滚动基线，每 20 秒判断一次。参数集中在以下环境变量，baseline 与 tick 必须是 bucket 的整数倍：

```env
HOTSPOT_BUCKET_S=10
HOTSPOT_BASELINE_WINDOW_S=90
HOTSPOT_DETECTOR_TICK_S=20
HOTSPOT_MIN_BASELINE_BUCKETS=3
HOTSPOT_DETECTION_THRESHOLD=0.55
HOTSPOT_EVENT_MERGE_GAP_S=30
HOTSPOT_EVENT_CONFIRM_DELAY_S=60
HOTSPOT_EVENT_SEMANTIC_OVERLAP_THRESHOLD=0.20
HOTSPOT_RECORDING_GAP_TOLERANCE_S=1
HOTSPOT_ENRICHMENT_LLM_MAX_TOKENS=65536
HOTSPOT_ASR_ENABLED=true
HOTSPOT_ASR_PRE_ROLL_S=35
HOTSPOT_ASR_POST_ROLL_S=55
HOTSPOT_ASR_PRIORITY=10
NEAR_LIVE_ASR_PRIORITY=50
BACKGROUND_ASR_PRIORITY=100
```

## 信号与缺失证据

- 弹幕：数量、独立用户、速度变化、爆发强度、重复集中度、情绪强度、高情绪词与代表文本。B 站接收延迟由 `DANMAKU_EVENT_LAG_S` 校正后再入桶。
- 音频：RMS 均值/峰值、能量变化、静音变化和局部峰值。
- SenseVoice：笑声、掌声、惊讶、情绪、音乐及其他带时间范围的音频事件。
- ASR：有词级时间戳时提供关键词、语速、词汇新颖度、实体/主题变化代理；缺失或尚未完成时不阻止检测。
- Trend：启用本地趋势资料库且当前桶有 ASR 正文时复用缓存匹配结果。

各模态按直播自身历史计算相对变化。可用信号使用动态权重归一化；不可用模态不会被伪装成零分拉低结果。为保证 provisional 层高召回，单个极强模态也可触发记录，但不会篡改四项解释分。每条热点分别保存 `heat_score`、`clip_score`、`semantic_confidence` 与 `evidence_coverage`，并保留可扩展的 evidence item。evidence 的 `type` 是开放字符串，后续可直接增加 `visual` 而无需改表。

## 幂等与事务

稳定事件键由场次、检测器版本与峰值 tick 生成。同一分析任务重试会复用并刷新尚未进入候选链的 provisional 记录；写入与分析任务状态推进共享一个数据库事务，租约失效时不会留下热点。

事件生命周期协调同样在该事务内完成。新事件先进入 `provisional`，同键观测或跨窗合并继续扩展边界时进入 `enriching`。相邻事件只有在录制媒体连续，且时间重叠或语义/信号连续时才会合并；跨分段的后继键保留为 `status=merged` 的别名并通过 `merged_into_id` 指向最早主事件，已排队的局部 ASR 因此仍能写回正确主事件。活动直播中，观察时间超过最新事件结尾 60 秒后进入 `confirmed`；已确认事件如果再次向前或向后扩展会重新进入 `enriching`，避免把仍在继续的爆点提前冻结。场次已经停止时，最后一次观察会直接确认剩余事件。没有新热点的后续分段也会推进确认状态。

代表弹幕不再只取全局频次前两名。选择器会确定性地保留一条高频 reaction、一条与人物/话题相关的信息型内容，以及容量允许时的一条幽默代表；每条仍保存出现次数，并在事件内部额外记录选择角色。

## EventEnricher 与证据约束

EventEnricher 只回答“刚才发生了什么”，不修改 `heat_score`、`clip_score`、事件边界或生命周期。它把事件局部 ASR、相邻分段转写、代表弹幕、音频、SenseVoice、趋势及未来可扩展证据整理为带稳定 `evidence_id` 的内容寻址 Evidence Bundle，再要求 LLM 返回严格的 `title/summary/category/entities/semantic_confidence/evidence_ids` JSON。

提交前会校验字段集合、类别、长度、实体与数字是否出现在被引用证据中、证据引用是否真实可用，以及叙述与证据的词面覆盖。仅由弹幕支持的说法必须明确归因给“弹幕/观众”；只有音频等非文本信号时拒绝生成具体事实。语义置信度按 ASR 质量与证据覆盖率设置上限，不能用热度替代。LLM 不可用或输出越界时写入保守回退摘要；LLM 调用期间证据发生变化则 fingerprint 不匹配，陈旧结果不会提交。实体与证据引用保存在 `features_json.event_enrichment`，无需增加数据库列。

## ClipScorer 与既有成片链

`ClipScorer` 只处理已经稳定为 `confirmed` 的完整事件，不再从五分钟分段的单个最大音频峰值直接决定成片。评分同时考虑完整事件内的热度、检测器成片先验、互动反应、持续时长、事件完整度、语音完整度、语义新颖度、音频强度、证据覆盖和录制连续性；同一模态跨多个 tick 使用事件范围内的均值与峰值组合，不会只取某一瞬间。缺失 ASR 不会阻断强弹幕/音频/SenseVoice 事件；只有语义很强而热度与互动反应都弱时，默认分数会保守封顶，因此热点可以保留但不会自动等同于视频候选。

只有事件级 `clip_score` 达到房间 `highlight_threshold` 才创建或复用 `HighlightCandidate` / `HighlightEvent`，并把 `HotspotEvent.candidate_id` 关联到既有审核、渲染和发布链。低于阈值的事件仍保留在时间线，`candidate_id` 继续为空。候选特征会保存热点 ID、事件版本、全部信号分、语义置信度、证据覆盖、ASR 质量、代表弹幕、动态边界和证据指纹，便于审核追踪。

候选边界从事件语义起止点向外增加 `config/scoring.yaml` 的前后留白，再只向外吸附到自然静音点；最终限制在峰值所在的连续录像块内，并受 `CLIP_MAX_DURATION_S` 约束。遇到断流缺口时绝不跨越缺口。IoU 去重仍适用于所有候选；冷却时间对旧分段评分保持原行为，对事件级候选则只压制同一语义事件，避免一分钟内两个不同事件被纯时间距离误合并。事件补全和评分都在事务外计算，提交时重新校验证据与配置指纹，以免重试或并发更新写入陈旧候选。

## ASR 双向融合与降级语义

流水线先用非语义信号执行检测，不再要求 `Transcript` 先存在。热点峰值会生成相对原始分段的 attention window，默认覆盖峰值前 35 秒到后 55 秒，并按以下顺序领取 ASR 工作负载：

1. 热点局部 ASR（priority 10），快速补充当前事件语义；
2. 近实时完整 ASR（priority 50）；
3. 历史/后台完整 ASR（priority 100），继续服务历史记录、搜索、字幕和整场总结。

局部识别与完整识别是同一 `SegmentTask` 的两个持久阶段，复用现有租约、资源预算和幂等键；局部证据提交后会重新排队完整识别，不会在同一个 Worker 调用内串行占用资源。局部结果以稳定 evidence id 写回 `HotspotEvent`，检测器重跑时会保留这项证据。

ASR 质量只决定语义证据状态：

- 可用正文记为 `available`，可进入关键词、语速、趋势和 LLM；
- 低质量正文记为 `degraded` 并保留诊断，不进入语义评分或 LLM；
- 后端耗尽重试记为 `unavailable`，任务仍进入候选分析。

在 `degraded` 或 `unavailable` 状态下，弹幕、音频和 SenseVoice 仍可独立形成高热度事件；由于缺少可靠语义，后续标题与摘要必须采用保守表述。
