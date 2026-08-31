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
