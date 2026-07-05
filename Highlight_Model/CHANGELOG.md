# Changelog — Highlight_Model

## v0.1.10.1-HL-Alpha (2026-07-04)

### 概念设计

- **特征全集梳理**: 基于母仓库 `app/analysis/` 全部模块 (`audio.py`、`transcribe.py`、`keywords.py`、`highlight.py`、`llm.py`、`topic_cluster.py`、`threshold_learning.py`) 的现有数据产出能力，系统梳理出 **103 项候选特征**，分为 6 大家族：
  - **声学特征** — 38 项（RMS 统计、频谱质心/带宽/滚降、13 维 MFCC、过零率、基频均值/标准差/范围、谐噪比、jitter/shimmer）
  - **语义/语言特征** — 21 项（语速、关键词、情感 5 类、文本 embedding、主题一致性、信息密度）
  - **弹幕交互特征** — 13 项（窗口速率、基线速率、加速度、爆发计数、文本熵、去重人数、跨模态时差）
  - **时序/上下文特征** — 9 项（邻段对比、滑动窗口、突变率）
  - **元数据/画像特征** — 11 项（主播 ID、历史高光密度、批准率、周期编码）
  - **跨模态融合特征** — 6 项（音量x弹幕、语速x弹幕、ASR-弹幕一致度）
- **训练标签定义**: 5 项目标变量（二值高光标签、多级质量评分、播出互动、审批日志、边界偏差）
- **工程阶段规划**: 9 阶段渐进式路线（概念→特征提取→训练→评估→接入→自学习→边界回归→质量评分→生产化）
- **母仓库接口约定**: 定义 ML 模型 `feature_extractor.extract()` + `model.predict_proba()` 可插拔替换现有 `score_segment()`
- **目录结构规划**: `feature_extractor/` + `dataset/` + `models/` + `tests/` + `notebooks/`

### 设计文档

- 新增 `README.md`: 103 项特征大表 + 工程阶段规划 + 接口约定 + 目录结构
- 新增 `CHANGELOG.md`: 本文件

### 分支信息

- 分支名: `Highlight_Model`
- 基于: `main` @ `5d0405e` (V0.1.8.1d Alpha)
- 版本号: `v0.1.10.1-HL-Alpha`（完整工程架构 + XGBoost模型 + 自学习引擎）
- 状态: 阶段 1-3 已完成，待模型评估与生产接入
