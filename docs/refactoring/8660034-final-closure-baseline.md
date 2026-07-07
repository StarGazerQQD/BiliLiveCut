# 稳定性封口基线 — 8660034

## 日期
2026-07-07

## 当前 Commit
8660034b8d095f4c6972ea5dd78bfec0fe51dcfc ("fix(ci): CI lint 阻断修复 + JS/版本检查")

## 当前应用版本
0.1.14.3-alpha

## 当前 Schema version
1

## pytest 收集数量
290 tests (21 files)

## pytest 通过/失败/跳过
- 通过: 290
- 失败: 0
- 跳过: 0

## Ruff
- ruff check: All checks passed!
- ruff format --check: 178 files already formatted

## JavaScript
- Python: app/web/static/app.js: PASS
- Python: app/web/static/app.js: PASS
- app/web/static/js/common.js: PASS
- app/web/static/js/dashboard.js: PASS
- app/web/static/js/recording.js: PASS
- app/web/static/js/review.js: PASS
- app/web/static/js/clips.js: PASS
- app/web/static/js/publishing.js: PASS
- app/web/static/js/settings.js: PASS
- app/web/static/js/monitor.js: PASS
- app/web/static/js/candidates.js: PASS
- app/web/static/js/rooms.js: PASS

## 版本一致性
- pyproject.toml: 0.1.14.3-alpha [OK]
- README.md: V0.1.14.3 Alpha [OK]
- CHANGELOG.md: ## V0.1.14.3 Alpha (2026-07-07) [OK]
- runtime: 0.1.14.3-alpha [OK]

## CHANGELOG 完整性
- 当前 CHANGELOG: 17 个版本 -> 系列 [12, 13, 14]
- 归档文件: 12 个
- 校验通过!

## 关键导入检查
- import app: OK (0.1.14.3-alpha)
- import app.db.models: OK
- import app.pipeline.task_worker: OK
- import app.pipeline.workers.analyze: OK
- import app.pipeline.workers.render: OK
- import app.pipeline.workers.publish: OK
- import app.pipeline.stale_recovery: OK
- import app.publishing.uploader: OK

## 当前数据库关键索引/唯一约束

### HighlightCandidate (`highlight_candidates`)
- PK: id
- INDEX: session_id
- INDEX: dedup_hash
- **无业务唯一约束** (仅 dedup_hash 有普通 index, 无 UNIQUE)

### HighlightEvent (`highlight_events`)
- PK: id
- UNIQUE: candidate_id (uq_highlight_event_candidate) ✓
- INDEX: session_id, topic_id
- FK: candidate_id → highlight_candidates.id

### HighlightTopic (`highlight_topics`)
- UNIQUE: (event_id, topic_id) (uq_topic_event_membership) ✓

### FinalClip (`final_clips`)
- PK: id
- INDEX: candidate_id, content_hash

### ClipVariant (`clip_variants`)
- PK: id
- UNIQUE: (event_id, variant_type, render_config_hash) (uq_clip_event_variant_config) ✓
- INDEX: event_id, candidate_id

### UploadTask (`upload_tasks`)
- PK: id
- UNIQUE: (clip_id, uploader) (uq_upload_target) ✓
- INDEX: clip_id

### UploadAttempt (`upload_attempts`)
- PK: id
- UNIQUE: attempt_token (单字段 unique)
- UNIQUE: (clip_id, attempt_token) (uq_upload_attempt)
- INDEX: upload_task_id, clip_id
- **唯一键基于随机 token, 非业务 Generation**
- 缺少: upload_task_id + publish_generation 业务唯一约束

## 当前 Candidate/Event 唯一语义
- Candidate: 仅 dedup_hash 有普通 index, **无 UNIQUE 约束**
- Event: candidate_id 有 UNIQUE 约束 (uq_highlight_event_candidate) ✓

## 当前 ClipVariant 唯一语义
- (event_id, variant_type, render_config_hash) 三维 UNIQUE ✓

## 当前 UploadAttempt 唯一语义
- attempt_token UNIQUE (随机 token)
- (clip_id, attempt_token) UNIQUE (依赖随机 token)
- **缺少业务排他键**: upload_task_id + publish_generation

---

## 4.1 测试差异分析 (82015b8 vs 8660034)

### 82015b8 (P8 最终验证, 重做)
同 8660034 测试文件完全一致。两个 commit 之间的 CI lint 修复 (ruff format) 未改变任何测试文件。

### 结论
- 消失的测试: 0
- 新增的测试: 0
- 重命名或移动的测试: 0
- pytest 未收集项: 0
- 文件一致, 测试数量同: 290

"294→290" 的差异并非当前这两个 commit 之间产生，属于更早历史记录中的描述差异。当前 HEAD 测试覆盖完整无丢失。
