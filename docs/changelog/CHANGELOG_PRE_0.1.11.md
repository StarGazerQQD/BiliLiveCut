# CHANGELOG — 0.1.11 系列

---

## V0.1.11 Alpha (2026-07-06)

### 数据一致性与流水线稳定性

本次版本不增加新 Feature/平台/LLM/原生加速模块。核心主题是修复候选、事件、主题、成片之间的数据关系。

---

#### 一、核心数据模型语义修正

- **HighlightCandidate**: 仅为机器分析结果,不等同于最终语义事件
- **HighlightEvent**: `candidate_id` 作为外键指向 `HighlightCandidate.id`;自动创建于候选评分完成后
- **ClipVariant.event_id**: 始终保存真实 `HighlightEvent.id`(不再是 `HighlightCandidate.id`)
- **HighlightTopic.event_id**: 永远指向真实 `HighlightEvent.id`
- 新增 `confirmed_by_user` 字段防止自动聚类静默覆盖人工确认
- SQLite 启用 `PRAGMA foreign_keys=ON`
- 新增 `_resolve_event_id()` / `_ensure_event()` helper,幂等创建 Event

#### 二、五开关独立生效

`auto_record / auto_analyze / auto_render / auto_approve / auto_upload` 逐阶段独立判断,不退化回 `manual/semi/auto`:
- **配置读取**: 每次阶段转换时重读房间配置(非一次性快照)
- **auto_analyze=false**: 登记分段但不创建分析任务
- **auto_render=false**: 批准后不自启渲染,需手动触发
- **auto_approve**: 需同时满足 `auto_approve=true` + 分数≥阈值 + 无敏感内容
- **auto_upload=false**: 任何代码路径不得自动投稿
- 非法状态转换直接拒绝

#### 三、任务 Worker 重构 — 真正并发

- 独立 `asyncio.create_task` + `set` 管理各阶段执行集合,不串行阻塞
- 环境变量 `MAX_TRANSCRIBING / MAX_ANALYZING / MAX_RENDERING / MAX_PUBLISHING` 控制并发

#### 四、原子任务领取

`_pop_and_claim`: SELECT + 原子赋值,每个任务只有一个 Worker 领取成功。

#### 五、attempts 只增一次

`mark_active` 仅在 `_pop_and_claim` 中调用一次,不在各阶段执行函数中重复计数。

#### 六、failed_stage 精确恢复

`failed_stage` 精确记录失败阶段(TRANSCRIBING/ANALYZING/RENDERING),重试时通过 `_resume_stage` 恢复到对应队列。不解析 `idempotency_key` 字符串。

#### 七、重试退避 + 错误分类

- `mark_failed` 区分 `transient`/`permanent`
- `_retry_expired` 从 `failed_stage` 恢复并重新入队
- `retry_task()` 自动/手动重试统一入口

#### 八、心跳和 Stale 恢复

- `heartbeat_at`: 长任务定期心跳
- `_recover_stale`: 启动时扫描超时任务标记为 stale 并重新入队
- 配置: `_HEARTBEAT_INTERVAL_S=30`, `_STALE_TIMEOUT_S=120`

#### 九、数据库迁移 `migrate_v011.py`

旧数据 `ClipVariant.event_id`/`HighlightTopic.event_id` 从 Candidate ID 转换为真实 Event ID,输出统计。

#### 十、新增模型字段

| 表 | 字段 | 说明 |
|----|------|------|
| `segment_tasks` | `event_id` | 关联 `highlight_events.id` |
| `segment_tasks` | `failed_stage` | 失败时的阶段 |
| `segment_tasks` | `claimed_by` | 领取 Worker ID |
| `segment_tasks` | `claimed_at` | 领取时间 |
| `segment_tasks` | `heartbeat_at` | 最后心跳时间 |
| `highlight_topics` | `confirmed_by_user` | 人工确认标记 |

#### 测试

178 项全部通过(17 项新增)。

---

---

历史版本归档见 [docs/changelog/CHANGELOG_INDEX.md](docs/changelog/CHANGELOG_INDEX.md)。

