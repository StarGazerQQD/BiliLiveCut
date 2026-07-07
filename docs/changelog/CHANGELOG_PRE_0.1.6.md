# Changelog — 0.1.6 系列 (已归档)

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

## V0.1.6 Alpha (2026-07-03)

### P0 重构
- **弹幕热度评分修复**:窗口速率与基线速率完全分离,基线使用分桶中位数,窗口内中心加权,Sigmoid 函数映射 0-1。新增可解释字段(窗口条数/速率/基线速率/倍数)。
- **自动化开关拆分**:废弃 `mode`(manual/semi/auto),新增 5 个独立开关:`auto_record` / `auto_analyze` / `auto_render` / `auto_approve` / `auto_upload`。新增两个阈值:`auto_approve_threshold`(≥自动批准)、`review_threshold`(≥进入审核,<自动淘汰)。旧 mode 自动迁移到新开关。
- **弹幕基线计算**:`:func:`_danmaku_baseline` 使用窗口前 20 分钟历史数据,按 10 秒分桶取中位数速率。
- **弹幕可解释数据**:`:func:`danmaku_score_explain` 返回审核页面可用的窗口速率、基线速率、比值等信息。
- **审核状态自动决定**:`score_segment` 根据房间 `auto_approve_threshold` / `review_threshold` 自动设置候选的初始状态。
- **流水线房间感知**:`make_pipeline_callback` 读取房间级 `auto_analyze` / `auto_render` / `auto_upload` 开关,分别控制各阶段。

### P0 基础设施
- **pip 镜像源调整**:默认源→阿里云 PyPI 镜像,备用源→清华大学镜像。支持 `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` 环境变量覆盖。更新 `pip.ini`、`launcher.py`、`build_bundle.py`、`.env.example`。
- **持久化任务队列**:新增 `SegmentTask` 模型与 14 个阶段状态(`RECORDED`→`QUEUED_FOR_TRANS`→`TRANSCRIBING`→...→`COMPLETED`/`FAILED`/`CANCELLED`)。阶段独立执行、独立重试,幂等键防重复。GPU 转写和 FFmpeg 渲染分别控制并发数。
- **任务 Worker (`app/pipeline/task_worker.py`)**:异步轮询调度器,崩溃恢复(启动时回退中间状态、补充孤立片段),指数退避重试,临时/永久失败区分。`retry_task()` / `cancel_task()` 提供手动干预。
- **流水线解耦**:录制回调只创建 `SegmentTask` 登记到队列,不再同步等待转写/分析/渲染。Web 生命周期启动/停止 Worker。
- **API 新增**:`GET /api/tasks`(任务列表+统计)、`POST /api/tasks/{id}/retry`、`POST /api/tasks/{id}/cancel`。
- **前端新增**:"任务队列"Dashboard 选项卡,顶部导航栏显示任务积压数,支持手动重试和取消。
- **数据库迁移**:`live_rooms` 表新增 7 列(`auto_record`/`auto_analyze`/`auto_render`/`auto_approve`/`auto_upload`/`auto_approve_threshold`/`review_threshold`)及旧 mode→新开关的兼容迁移。新增 `segment_tasks` 表。
- `launcher.exe` 重新编译同步。

### P0 测试
- 弹幕评分:`danmaku_rate_score` Sigmoid 映射、基线保护、除零保护。`fuse_scores` / `weighted_rule_score` 融合/加权。
- 状态机:13 个合法转换、6 个非法转换验证,终态不出转换。幂等键 `segment_id:stage` 格式。
- 队列推进:`enqueue_next` 合法转换、非法→ValueError。

### P1 横屏审片工作台 + 主题识别
- **HighlightEvent 数据模型**(`highlight_events`):独立的高光事件,含原始/人工调整边界、细粒度审核决断(14种)、主题归属、审核原因、ASR 文本留存。
- **ClipVariant 数据模型**(`clip_variants`):同一事件的多版本成品(single/full_context/collection_chapter/subtitled/no_subtitles/compressed/archive)。
- **Topic + HighlightTopic 模型**:主题聚类与事件-主题多对多关联,支持 auto/confirmed/split/blocked 状态,is_collection 合集标记。
- **审片工作台**(`/review/{candidate_id}`):16:9 横屏视频播放器、弹幕密度 Canvas 图(5s桶)、评分维度贡献柱状图、弹幕解释文本。键盘快捷键(Space/I/O/J/K/L/←→)、入/出点 ±3/5/10/30s 调整按钮、重新渲染、上一个/下一个候选导航。
- **细粒度审核决断**(`ReviewStatus`):14种状态(独立成片/合集候选/保留待定/不够精彩/上下文不足/开头截晚/结尾截早/内容重复/字幕错误/画面异常/敏感内容/拒绝等)。审核原因和人工边界持久化。
- **主题识别模块**(`app/analysis/topic_cluster.py`):基于 ASR 文本字符级 bigram TF-IDF 余弦相似度(权重55%)+关键词重叠(权重25%)+时间衰减(权重20%)的综合相似度计算。阈值分层:≥0.82自动归组、0.60-0.82人工确认、<0.60独立。Union-Find 聚类算法。
- **主题 API**:`GET /api/topics`(列表)、`GET /api/topics/{id}`(详情)、`PATCH /api/topics/{id}`(更新)、`POST/DELETE /api/topics/{id}/events/{eid}`(加入/移除)、`POST /api/topics/merge`(合并)、`POST /api/topics/{id}/split`(拆分)、`POST /api/topics/{id}/reorder`(重排)、`POST /api/sessions/{id}/cluster`(触发聚类)。
- **ClipVariant API**:`GET /api/events/{id}/variants`(列出某事件的所有版本)。
- **审片 API**:`GET /review/{candidate_id}`(页面)、`GET /review/api/{candidate_id}`(数据)、`POST /review/api/{candidate_id}/adjust`(调整边界)、`POST /review/api/{candidate_id}/review`(提交审核)、`POST /review/api/{candidate_id}/rerender`(重新渲染)。
- **Dashboard 集成**:候选审核卡片新增"🎬 审片"链接(新窗口打开工作台)；新增"主题管理"选项卡(选择会话触发聚类、查看主题列表、标记合集)。
- **数据库**:新增 `highlight_events`/`clip_variants`/`topics`/`highlight_topics` 四张表(SQLModel 自建)。

### P1 测试
- 主题相似度:文本相似度5项(相同/相近/无关/空/单字)、关键词重叠4项(全重叠/部分/无/空)、余弦相似度3项(相同/正交/空)、事件综合相似度3项(相同/不同主题/空字段)、阈值常量2项,共 **17 项全部通过**。
- 全量回归 **126 项全部通过**(109旧+17新),无回归。
