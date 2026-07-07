# 基线文档: 82015b8

## 元数据

- **Commit**: `82015b898a3d10be4f457b86bcde044b1704edef`
- **时间**: 2026-07-07
- **分支**: `main`

## 版本号来源 (不一致)

| 位置 | 值 |
|------|-----|
| `app/__init__.py` | `0.1.14.3-alpha` |
| `pyproject.toml` | `0.1.14.2-alpha` |
| `README.md` | `V0.1.14.2 Alpha` |
| `CHANGELOG.md` | `V0.1.14.2 Alpha` (最新) |

**版本不一致: pyproject.toml / README / CHANGELOG 落后于 app.__version__。**

## 测试

- pytest 收集: 290 tests
- pytest 结果: 290 passed, 0 failed, 0 skipped
- Ruff: All checks passed

## Python 导入冒烟

- `import app` — 通过
- `import app.pipeline.task_worker` — 通过
- `import app.pipeline.workers.analyze` — 通过
- `import app.pipeline.workers.render` — 通过
- `import app.pipeline.workers.publish` — 通过
- `import app.web.main` — 通过
- `import app.cli` — 通过

## JavaScript 语法检查

- `app/web/static/app.js` — **通过** (exit 0)
- `app/web/static/js/api.js` — **通过 (但内容为 Placeholder)**
- `app/web/static/js/common.js` — **通过**
- `app/web/static/js/dashboard.js` — **通过**
- `app/web/static/js/recording.js` — **通过**
- `app/web/static/js/review.js` — **通过**
- `app/web/static/js/clips.js` — **通过**
- `app/web/static/js/publishing.js` — **通过**
- `app/web/static/js/settings.js` — **通过**
- `app/web/static/js/monitor.js` — **通过**
- `app/web/static/js/rooms.js` — **通过** (额外发现)

**当前无 JS 语法错误。`review.js` 语法检查通过。**

## 已知问题

### 2.1 分析阶段
- `analyze.py:_score_segment_draft()` 在 compute 路径中调用 `_mark_scored(segment_id)` (第 278, 288, 311 行)
- 低分、重复、跳过分支均执行 DB 写操作
- compute 并非纯计算

### 2.2 渲染阶段
- `render.py:render_compute()` 调用 `produce_clip()` (第 69 行)
- `produce_clip()` 直接生成正式文件并写入 Clip/ClipVariant
- 未使用 lease 临时路径 (虽然 `_temp_clip_path()` 已定义但未使用)

### 2.3 发布阶段
- `publish.py:publish_compute()` 调用 `enqueue_and_upload()` (第 57 行)
- 远程上传在 compute 阶段发生
- `remote_result_unknown` 状态只存在于内存字典，未持久化到 DB
- 无 UploadAttempt 模型

### 2.4 生命周期
- `lifecycle.py:_shutting_down: bool = False` — 模块级 bool
- `task_worker.py` 使用 `global _shutting_down` 重新赋值
- `heartbeat.py` 通过 `from app.pipeline.lifecycle import _shutting_down` 读取
- 跨模块 bool 重新赋值导致不同模块看到不同状态

### 2.5 前端
- `api.js` 是 Placeholder (仅 `// Placeholder` 一行)
- API 封装实际由 `common.js` 提供

### 2.6 阶段 run 函数
- `run_render()` 第 140-142 行: 无条件 `mark_heartbeat(task)` + `db.commit()`
- `run_publish()` 第 135-137 行: 无条件 `mark_heartbeat(task)` + `db.commit()`

## Worker 副作用调用链

```
analyze_compute → _score_segment_draft() → _mark_scored()    [DB write]
                                          → _is_duplicate()   [DB read + hash]
render_compute  → produce_clip()         → Clip/CV create    [DB + file]
publish_compute → enqueue_and_upload()   → remote HTTP call  [remote]
```
