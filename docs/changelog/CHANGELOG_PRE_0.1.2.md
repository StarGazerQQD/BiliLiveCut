# Changelog — 0.1.2 系列 (已归档)

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

## V0.1.2 Alpha (2026-07-02)

### 新增

- **录制中断自动恢复**:Web 后台启动时自动扫描最近 24h 内中断的录制会话并恢复录制,
  有效应对进程崩溃/机器重启等场景
- **录制预约**:支持按时间计划自动启动录制(`blc schedule` CLI 命令),Dashboard 新增
  「录制预约」标签页,可创建/查看/删除预约;支持单次和每日重复
- **AI 阈值自学习**:用户审批/拒绝候选时自动记录评分与阈值快照,累计 10 条反馈后
  自动计算推荐阈值(P15 分位数),每房间独立学习,单次调整幅度上限 0.1
- **弹幕情绪分析**:基于弹幕文本的规则型情绪分析(重复率 + 感叹号密度 + 高情绪梗),
  作为高光评分的独立维度(`danmaku_sentiment`),完全离线,不依赖外部 API
- **流水线进度追踪**:Dashboard 录制状态页新增进度条,实时展示已录制/已转写/已评分
  片段数量与进度百分比
- **Dashboard 功能开关**:每个直播间卡片新增「预约录制」「阈值自学习」「弹幕情绪」
  三项开关,录制启动后自动锁定(不可更改,防止状态冲突)

### 变更

- 数据库模型新增 ``RecordingSchedule``、``ThresholdFeedback`` 两张表;``LiveRoom``
  表新增 ``schedule_enabled`` / ``auto_threshold_enabled`` / ``danmaku_sentiment_enabled`` 字段
- ``RecordingSession`` 表新增 ``last_reconnected_at`` 字段用于追踪重连成功时间
- 评分配置 ``scoring.yaml`` 增加 ``danmaku_sentiment`` 维度(权重 0.15)
- ``SessionStatus`` 新增 ``INTERRUPTED`` / ``RECONNECTED`` 状态
- 后端 ``init_db()`` 现已包含轻量迁移逻辑(为旧表补充缺失列)

### 修复

- **超管断流重连优化**:断流重连成功后首个片段写入即重置退避计数器(backoff→1),
  避免"稳定录制 30 分钟后再次被断流,却要白等 30s"。Dashboard 录制状态页
  现在展示最近重连成功时间与 ``RECONNECTED`` 绿色徽章。
