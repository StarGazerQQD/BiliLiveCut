# Highlight_Model — BiliLiveCut 自有机器学习高光模型

**当前版本: v0.1.13.1-HL-Alpha** (`0.1.13.1-HL-alpha`)

本分支为 [BiliLiveCut](https://github.com/StarGazerQQD/BiliLiveCut) 母仓库的阶段 2（高光判断）设计并实现一个**自有机器学习高光模型**，替代现有的"规则+LLM 复核"混合管线，在保持零费用、完全本地化的前提下，提升高光识别的准确率与召回率。

---

## 设计目标

| 目标 | 说明 |
|---|---|
| **完全本地化** | 不依赖任何外部 LLM API，推理可在 CPU 上完成 |
| **向后兼容** | 作为现有 `HighlightCandidate` 评分管线的可插拔替代，输入/输出接口一致 |
| **高于规则基线** | 在人工审批数据上，AUC / F1 显著优于纯规则 `rule_score` |
| **增量可用** | 初始版本可用现有 `ThresholdFeedback` 表作为训练标签，后续可引入人工评分 |
| **可解释** | 每个预测附带特征重要性（SHAP / 内置 importance），审核页可直接展示 |

---

## 特征全集 TODO（共 103 项）

### 一、声学特征（Audio Acoustic）— 38 项

| # | 特征 ID | 名称 | 说明 | 状态 |
|---|---------|------|------|------|
| A1 | `rms_mean` | RMS 均值 | RMS 能量包络的均值 | ⬜ |
| A2 | `rms_median` | RMS 中位数 | RMS 能量包络的中位数 | ⬜ |
| A3 | `rms_std` | RMS 标准差 | RMS 能量包络的标准差 | ⬜ |
| A4 | `rms_p25` | RMS P25 | RMS 第 25 百分位数 | ⬜ |
| A5 | `rms_p75` | RMS P75 | RMS 第 75 百分位数 | ⬜ |
| A6 | `rms_p90` | RMS P90 | RMS 第 90 百分位数 | ⬜ |
| A7 | `crest_factor` | 峰均比 | peak / RMS mean | ⬜ |
| A8 | `energy_entropy` | 能量熵 | RMS 分布的 Shannon 熵 | ⬜ |
| A9 | `short_term_energy_ratio` | 短窗能量比 | 1s 短窗峰值能量 / 整段平均能量 | ⬜ |
| A10 | `rms_delta_max` | RMS 最大跳变 | 相邻帧 RMS 的最大正向跳跃 | ⬜ |
| A11 | `silence_ratio` | 静音占比 | 静音帧占总帧数的比例 | ⬜ |
| A12 | `silence_count` | 静音段数 | 静音区间的个数 | ⬜ |
| A13 | `avg_silence_duration` | 平均静音时长 | 所有静音段的平均持续时长 | ⬜ |
| A14 | `pause_before_peak` | 爆前静音 | 峰值前的最近静音段时长 | ⬜ |
| A15 | `peak_slope` | 爆点斜率 | 爆点前后 RMS 上升/下降速率 | ⬜ |
| A16 | `spectral_centroid` | 频谱质心 | 声音明亮度 | ⬜ |
| A17 | `spectral_bandwidth` | 频谱带宽 | 频谱能量分散程度 | ⬜ |
| A18 | `spectral_rolloff` | 频谱滚降 | 频谱能量集中度 | ⬜ |
| A19 | `mfcc_1` | MFCC-1 | 梅尔频率倒谱系数第 1 维 | ⬜ |
| A20 | `mfcc_2` | MFCC-2 | 梅尔频率倒谱系数第 2 维 | ⬜ |
| A21 | `mfcc_3` | MFCC-3 | 梅尔频率倒谱系数第 3 维 | ⬜ |
| A22 | `mfcc_4` | MFCC-4 | 梅尔频率倒谱系数第 4 维 | ⬜ |
| A23 | `mfcc_5` | MFCC-5 | 梅尔频率倒谱系数第 5 维 | ⬜ |
| A24 | `mfcc_6` | MFCC-6 | 梅尔频率倒谱系数第 6 维 | ⬜ |
| A25 | `mfcc_7` | MFCC-7 | 梅尔频率倒谱系数第 7 维 | ⬜ |
| A26 | `mfcc_8` | MFCC-8 | 梅尔频率倒谱系数第 8 维 | ⬜ |
| A27 | `mfcc_9` | MFCC-9 | 梅尔频率倒谱系数第 9 维 | ⬜ |
| A28 | `mfcc_10` | MFCC-10 | 梅尔频率倒谱系数第 10 维 | ⬜ |
| A29 | `mfcc_11` | MFCC-11 | 梅尔频率倒谱系数第 11 维 | ⬜ |
| A30 | `mfcc_12` | MFCC-12 | 梅尔频率倒谱系数第 12 维 | ⬜ |
| A31 | `mfcc_13` | MFCC-13 | 梅尔频率倒谱系数第 13 维 | ⬜ |
| A32 | `zero_crossing_rate` | 过零率 | 浊音/清音区分 | ⬜ |
| A33 | `pitch_mean` | 基频均值 | 基频均值 Hz | ⬜ |
| A34 | `pitch_std` | 基频标准差 | 基频波动程度 | ⬜ |
| A35 | `pitch_range` | 基频范围 | 基频 max - min | ⬜ |
| A36 | `harmonic_noise_ratio` | 谐噪比 | 声音清晰度指标 | ⬜ |
| A37 | `jitter` | 基频微扰 | 周期间基频变化 | ⬜ |
| A38 | `shimmer` | 振幅微扰 | 周期间振幅变化 | ⬜ |

---

### 二、语义/语言特征（Linguistic）— 21 项

| # | 特征 ID | 名称 | 说明 | 状态 |
|---|---------|------|------|------|
| L1 | `text_length_chars` | 文本长度 | 转写文本字符数 | ⬜ |
| L2 | `word_count` | 词数 | 从 words_json 统计的词数 | ⬜ |
| L3 | `whisper_confidence` | 转写置信度 | avg_logprob | ⬜ |
| L4 | `speech_rate_wps` | 平均语速 | 词/秒 | ⬜ |
| L5 | `speech_rate_peak_ratio` | 语速峰值比 | 局部峰值语速 / 平均语速 | ⬜ |
| L6 | `pause_density` | 停顿密度 | 单位时间内的词间长停顿次数 | ⬜ |
| L7 | `keyword_hit_count` | 关键词命中数 | 高光关键词表命中条数 | ⬜ |
| L8 | `keyword_density` | 关键词密度 | 命中数 / 总字数 | ⬜ |
| L9 | `exclamation_ratio` | 感叹号占比 | !/！/? 占总字符比例 | ⬜ |
| L10 | `laughter_char_ratio` | 笑声字符比 | 哈/笑/草/233 密度 | ⬜ |
| L11 | `sentiment_score` | 情感极性 | 正/负/中性概率（本地情感模型） | ⬜ |
| L12 | `emotion_joy` | 喜悦概率 | 多维情绪分布 - joy | ⬜ |
| L13 | `emotion_surprise` | 惊讶概率 | 多维情绪分布 - surprise | ⬜ |
| L14 | `emotion_anger` | 愤怒概率 | 多维情绪分布 - anger | ⬜ |
| L15 | `emotion_sadness` | 悲伤概率 | 多维情绪分布 - sadness | ⬜ |
| L16 | `emotion_fear` | 恐惧概率 | 多维情绪分布 - fear | ⬜ |
| L17 | `topic_coherence` | 主题一致性 | 连续窗口内的 bigram 相似度 | ⬜ |
| L18 | `info_density` | 信息密度 | 命名实体出现频率 | ⬜ |
| L19 | `qa_pattern_flag` | 问答模式 | 是否存在一问一答式交互 | ⬜ |
| L20 | `filler_word_ratio` | 填充词占比 | 那个/就是/嗯/啊 占比 | ⬜ |
| L21 | `text_embedding_dim` | 文本 Embedding | 512 维 sentence embedding 降维预留 | ⬜ |

---

### 三、弹幕交互特征（Danmaku）— 13 项

| # | 特征 ID | 名称 | 说明 | 状态 |
|---|---------|------|------|------|
| D1 | `dm_window_count` | 窗口弹幕数 | 片段时间窗内弹幕总条数 | ⬜ |
| D2 | `dm_window_rate` | 窗口弹幕速率 | 条/秒 | ⬜ |
| D3 | `dm_baseline_rate` | 弹幕基线速率 | 前 20 分钟中位数分桶速率 | ⬜ |
| D4 | `dm_rate_ratio` | 速率比值 | 窗口速率 / 基线速率 | ⬜ |
| D5 | `dm_rate_acceleration` | 速率加速度 | 弹幕速率的二阶导数 | ⬜ |
| D6 | `dm_center_weighted_rate` | 中心加权速率 | 距片段中心越近权重越高 | ⬜ |
| D7 | `dm_burst_count` | 爆发次数 | 2s 短窗内最高弹幕密度 | ⬜ |
| D8 | `dm_text_entropy` | 弹幕文本熵 | 弹幕内容多样性 | ⬜ |
| D9 | `dm_exclaim_ratio` | 感叹弹幕比 | 含 !/！ 的弹幕占比 | ⬜ |
| D10 | `dm_meme_hit_ratio` | 梗命中率 | 高情绪梗关键词命中占比 | ⬜ |
| D11 | `dm_high_value_ratio` | 高价值弹幕比 | 超级留言/礼物弹幕占比 | ⬜ |
| D12 | `dm_viewer_unique` | 弹幕去重人数 | 不同 UID 的弹幕发送者数 | ⬜ |
| D13 | `dm_lead_lag_ms` | 弹幕-音频时差 | 弹幕爆发与音频爆点的平均时差 | ⬜ |

---

### 四、时序/上下文特征（Temporal & Contextual）— 9 项

| # | 特征 ID | 名称 | 说明 | 状态 |
|---|---------|------|------|------|
| T1 | `segment_duration_s` | 片段时长 | 秒 | ⬜ |
| T2 | `segment_size_bytes` | 文件大小 | 间接反映码率/画面复杂度 | ⬜ |
| T3 | `session_elapsed_ratio` | 直播进度比 | 当前片段在整场直播中的位置 0-1 | ⬜ |
| T4 | `time_since_last_highlight_s` | 距上高光间隔 | 秒 | ⬜ |
| T5 | `neighbor_volume_diff` | 邻段音量差 | 与前/后片段的音量差值 | ⬜ |
| T6 | `neighbor_dm_diff` | 邻段弹幕差 | 与前/后片段的弹幕速率差值 | ⬜ |
| T7 | `rolling_volume_avg` | 滑动音量均值 | 前 N 段滑动窗口平均音量 | ⬜ |
| T8 | `rolling_dm_avg` | 滑动弹幕均值 | 前 N 段滑动窗口平均弹幕速率 | ⬜ |
| T9 | `feature_change_rate` | 特征突变率 | 当前段与上一段各维度的变化率向量 | ⬜ |

---

### 五、元数据/画像特征（Metadata & Profiling）— 11 项

| # | 特征 ID | 名称 | 说明 | 状态 |
|---|---------|------|------|------|
| M1 | `streamer_id` | 主播 ID | 房间 room_id | ⬜ |
| M2 | `room_hist_highlight_rate` | 历史高光密度 | 该房间历史高光候选个/小时 | ⬜ |
| M3 | `room_approval_rate` | 历史批准率 | ThresholdFeedback 中的 approved 占比 | ⬜ |
| M4 | `room_current_threshold` | 当前房间阈值 | LiveRoom.highlight_threshold | ⬜ |
| M5 | `room_auto_approve_threshold` | 自动批准阈值 | LiveRoom.auto_approve_threshold | ⬜ |
| M6 | `time_of_day_sin` | 时段 sin | sin(2pi * hour / 24) 周期编码 | ⬜ |
| M7 | `time_of_day_cos` | 时段 cos | cos(2pi * hour / 24) 周期编码 | ⬜ |
| M8 | `day_of_week_sin` | 星期 sin | sin(2pi * weekday / 7) 周期编码 | ⬜ |
| M9 | `day_of_week_cos` | 星期 cos | cos(2pi * weekday / 7) 周期编码 | ⬜ |
| M10 | `stream_duration_minutes` | 直播已持续 | 分钟 | ⬜ |
| M11 | `config_hotword_count` | 房间热词数 | 房间配置中的 hotwords 条数 | ⬜ |

---

### 六、跨模态融合特征（Cross-Modal Fusion）— 6 项

| # | 特征 ID | 名称 | 说明 | 状态 |
|---|---------|------|------|------|
| C1 | `volume_x_danmaku` | 音量x弹幕 | 音量峰值与弹幕峰值的乘积 | ⬜ |
| C2 | `speech_rate_x_danmaku` | 语速x弹幕 | 语速突增与弹幕爆发的同步乘积 | ⬜ |
| C3 | `keyword_x_danmaku_meme` | 关键词-弹幕梗交集 | 文本关键词与弹幕梗的命中交集 | ⬜ |
| C4 | `silence_x_explosion` | 静默到爆发 | pause_before_peak * peak_slope | ⬜ |
| C5 | `asr_dm_similarity` | ASR-弹幕一致度 | 转写与弹幕文本的语义相似度 | ⬜ |
| C6 | `trend_match_score` | 网感关联度 | 片段题材与全网热门内容的关联 | ⬜ |

---

### 七、训练标签（Target Labels）— 5 项

| # | 标签 ID | 名称 | 来源 | 状态 |
|---|---------|------|------|------|
| G1 | `is_highlight` | 二值高光标签 | ThresholdFeedback.action == approved -> 1 | ⬜ |
| G2 | `highlight_quality` | 多级质量标签 | 人工1-5星评分（待引入） | ⬜ |
| G3 | `clip_engagement` | 播出互动数据 | 播放/点赞/投币（待接入） | ⬜ |
| G4 | `review_action_log` | 审批日志对 | (highlight_score, action) 完整记录 | ⬜ |
| G5 | `boundary_adjustment_delta` | 边界偏差 | adjusted_start_ts - raw_start_ts | ⬜ |

---

## 总计

| 家族 | 特征数 |
|------|--------|
| 声学 | 38 |
| 语义/语言 | 21 |
| 弹幕交互 | 13 |
| 时序/上下文 | 9 |
| 元数据/画像 | 11 |
| 跨模态融合 | 6 |
| 训练标签 | 5 |
| **总计** | **103** |

---

## 工程阶段规划

| 阶段 | 内容 | 版本 | 状态 |
|------|------|------|------|
| 0 | 概念设计与特征清单 | v0.1.13.1-HL-Alpha | ✅ 完成 |
| 1 | 特征提取管线 (FeatureExtractor) | v0.1.13.1-HL-Alpha | ✅ 完成 |
| 2 | 训练数据构建 (DatasetBuilder) | v0.1.13.1-HL-Alpha | ✅ 完成 |
| 3 | 模型训练 + 自学习引擎 | v0.1.13.1-HL-Alpha | ✅ 完成 |
| 1 | 特征提取管线 (FeatureExtractor) | v0.1.0 | ⬜ |
| 2 | 训练数据构建 (DatasetBuilder) | v0.2.0 | ⬜ |
| 3 | 模型选型与训练 (XGBoost / LightGBM / MLP) | v0.3.0 | ⬜ |
| 4 | 模型评估与阈值校准 | v0.4.0 | ⬜ |
| 5 | 接入现有管线 (score_segment 可插拔替换) | v0.5.0 | ⬜ |
| 6 | 模型自学习（在线更新 + A/B 测试） | v0.6.0 | ⬜ |
| 7 | 边界回归子模型（替代规则吸附） | v0.7.0 | ⬜ |
| 8 | 质量评分子模型（替代单一 highlight_score） | v0.8.0 | ⬜ |
| 9 | 生产化部署与母仓库合并 | v1.0.0 | ⬜ |

---

## 与母仓库的接口约定

本分支产出的模型通过以下接口接入母仓库 `app/analysis/highlight.py` 的 `score_segment()`：

```python
# 现有接口 (纯规则 + LLM)
features: dict[str, float] = {
    "volume": feats.volume_score(),
    "danmaku": _danmaku_score(...),
    "keywords": kw_score,
    "speech_rate": speech_rate_score(...),
    "laughter": laughter_score(text),
}
rule_score = weighted_rule_score(features, cfg.weights)
highlight_score = fuse_scores(rule_score, llm_score, cfg.alpha, cfg.beta)

# 目标接口 (ML 模型)
ml_features: np.ndarray = feature_extractor.extract(segment_id)  # shape: (103,)
highlight_score: float = model.predict_proba(ml_features)[1]     # 0-1
```

模型文件存放于 `storage/models/highlight_model.{pkl,onnx,pt}`，通过环境变量 `ML_MODEL_PATH` 配置。

---

## 目录结构（规划）

```
Highlight_Model/
├── README.md                  # 本文件
├── CHANGELOG.md               # 变更日志
├── feature_extractor/         # 特征提取模块
│   ├── __init__.py
│   ├── base.py                # BaseFeatureExtractor 抽象类
│   ├── acoustic.py            # 声学特征 (A1-A38)
│   ├── linguistic.py          # 语义特征 (L1-L21)
│   ├── danmaku.py             # 弹幕特征 (D1-D13)
│   ├── temporal.py            # 时序特征 (T1-T9)
│   ├── metadata.py            # 画像特征 (M1-M11)
│   └── fusion.py              # 跨模态特征 (C1-C6)
├── dataset/                   # 数据集构建
│   ├── __init__.py
│   ├── builder.py             # 从 ThresholdFeedback 构建训练集
│   └── preprocessor.py        # 缺失值/归一化/编码
├── models/                    # 模型定义与训练
│   ├── __init__.py
│   ├── train.py               # 训练入口
│   ├── evaluate.py            # 评估脚本
│   └── inference.py           # 推理接口
├── tests/                     # 单元测试
│   ├── __init__.py
│   ├── test_acoustic.py
│   ├── test_linguistic.py
│   └── ...
└── notebooks/                 # 探索性分析
    └── eda.ipynb
```
