# 基线快照 — Commit f501a3d

## 版本信息
- Commit: `f501a3d`
- 版本: `0.1.14.2-alpha`

## 测试
- 收集: **290 tests**
- 通过: **290**
- 失败: **0**
- 跳过: **0**

## Ruff
- check: **0 errors**
- format: **170 files already formatted**

## CLI 命令集合 (27 个)
```
init, add-room, list-rooms, check, record, transcribe, score,
process, list-candidates, clip, copywrite, produce, list-clips,
upload, set-upload, trends-collect, trends-list, trends-keywords,
db-reset, trends-purge, llm-list, llm-test, schedule, doctor, serve
```
位置: `app/cli.py` (889 lines) — 全部实现在单体文件中

## API 路由集合 (79 个)
```
GET  /analytics, /asr-metrics, /asr-models, /candidates, /clips,
     /clips/{id}/cover, /clips/{id}/video, /cookie-status, /dashboard,
     /events/{id}/variants, /llm-providers, /login/status, /logs,
     /metrics, /notifications, /progress, /recording, /rooms/{id}/threshold-learning,
     /schedules, /settings, /tasks, /topics, /topics/{id}, /transcripts,
     /trends, /uploads, ...
POST /candidates/batch, /candidates/{id}/approve, /rooms, /login, ...
PUT  /llm-providers, ...
DELETE /candidates/{id}, /schedules/{id}, ...
```
位置: `app/web/routers/` (18 个文件) — 已正确拆分

## 数据库元数据
- 表数量: **20**
- 表列表: `app_settings, clip_variants, danmaku, final_clips, highlight_candidates, highlight_events, highlight_topics, intro_templates, live_rooms, raw_segments, recording_schedules, recording_sessions, segment_tasks, subtitle_templates, system_logs, threshold_feedback, topics, transcripts, trend_items, upload_tasks`
- 模型文件: `app/db/models.py` (777 lines) — 全部实现在单体文件中
- 实体目录: `app/db/entities/` — 9 个文件，全部为 `from app.db.models import *` Placeholder

## 前端
- `app.js`: **48,755 bytes** — 全部逻辑在单体文件中
- `js/` 目录: 9 个文件，全部为 `// Placeholder`

## Worker
- `task_worker.py`: **994 lines**
- 四个阶段 Worker 已拆出至 `app/pipeline/workers/`（transcribe/analyze/render/publish）
- claiming, heartbeat, stale_recovery, lifecycle 仍集中在 task_worker.py

## 加速模块
- 重复源文件:
  - `app/analysis/_c_speedups.c` (29,107 bytes) — 旧位置
  - `app/accelerators/c/_c_speedups.c` (29,107 bytes) — 新位置
  - `app/analysis/_speedups_round2.pyx` (8,870 bytes) — 旧位置
  - `app/accelerators/cython/_speedups_round2.pyx` (8,870 bytes) — 新位置

## 旧迁移代码
- `app/db/migrate_v011.py` — 残留

## 模块版本号噪声
- 约 48 个模块 docstring 携带具体应用版本号 (V0.1.14.2)
