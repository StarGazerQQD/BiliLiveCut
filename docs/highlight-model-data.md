# 高光模型数据与特征契约

当前数据与特征公共层位于 `app.analysis.highlight_ml`。同一 Schema 同时用于离线训练和可选在线评分；在线模式、回退和审计契约见[训练与生命周期](highlight-model-lifecycle.md#主程序在线接入)。

训练、评估、模型注册与漂移契约见 [高光模型训练与生命周期](highlight-model-lifecycle.md)。

## 标签策略

- 正类：人工审核为 `approved_solo`、`approved_collection` 或 `in_collection`，以及 `ThresholdFeedback.action=approved`。
- 负类：人工审核为 `not_exciting` 或 `rejected`，以及 `ThresholdFeedback.action=rejected`。
- `start_too_late`、`end_too_early`、`subtitle_error`、`visual_issue` 等只说明边界或成片质量，不作为“非高光”标签。
- 同一片段存在多次明确决断时只保留时间最新的一次，避免重复样本跨训练集和验证集泄漏。
- 未审核片段不会自动视为负类，而是由 `blind_review_limit` 抽入确定性盲审队列。

候选优先通过 `HighlightEvent.segment_id` 关联原始片段；旧数据缺少该字段时，才按同一录制会话内 `peak_ts` 所在的片段时间区间兜底。

## 特征契约

Schema 版本为 `1.0.0`，包含 35 个原始连续特征。每个特征同时生成一个 `__available` 列，因此模型输入共 70 列。Schema 的版本、名称、分组、描述和缺失值策略共同生成 SHA-256 指纹；训练或推理侧出现列顺序/语义漂移时必须拒绝加载。

特征分为以下组：

- temporal：片段时长、会话内已过去时间；
- linguistic：字符速率、标点、笑声、词速、词时长和停顿；
- asr：平均对数概率、复核风险；
- danmaku：窗口数量/速率、历史热度比、用户与内容去重率、标点和价值权重；
- acoustic：一次音频解码聚合出的 RMS、突出度和静音占比；
- auxiliary：SenseVoice 笑声、掌声、惊讶和开心事件数。

缺失值始终编码为 `NaN`，对应 `__available` 为 `0`；真实零值的 availability 为 `1`。没有任何弹幕记录可证明采集有效时，弹幕组保持缺失，不用零伪造“没有互动”。

## 时间与查询边界

一个片段只构建一次 `SegmentFeatureContext`。转写、词时间戳、弹幕和可选音频快照由所有特征共享。弹幕查询最多读取片段前 600 秒到片段结束的范围，并在内存中划分历史基线与当前窗口；片段结束后的弹幕、会话结束状态、候选分数和审核结果都不进入特征。
