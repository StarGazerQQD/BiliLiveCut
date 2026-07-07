# Changelog — 0.1.3 系列 (已归档)

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

## V0.1.3 Alpha (2026-07-02)

### 修复

**Bug 审计修复(审计范围:38 个源文件,共修复 26 个问题)**

- **CRITICAL**: OpenReviewAI 客户端改为模块级单例缓存,避免长时间录制耗尽连接池 (`llm.py:_get_client`)
- **CRITICAL**: `active_providers()` 增加 `base_url` 非空检查,防止空 URL 静默调用 OpenAI 官方 API (`llm_providers.py`)
- **CRITICAL**: 上传任务/裁剪偏移增加 `None` 检查,避免 `db.get()` 返回 `None` 时 `AttributeError` (`uploader.py`, `clipper.py`)
- **HIGH**: `danmaku_sentiment_score` 移除死代码(全表查询后丢弃),`_fetch_window_danmaku_texts`/`_danmaku_score` 改为 SQL 级时间过滤,消除全表扫描 O(n²) 性能退化 (`highlight.py`)
- **HIGH**: `Recorder._registered_paths` 改为内存缓存,每片段不再查全表 (`recorder.py`)
- **HIGH**: 弹幕写入改用 `add_all()` 批量插入,WebSocket 超时从 40s 降至 35s (`danmaku.py`)
- **MEDIUM**: `Recorder._seq` 在每次 `run()` 开始时复位,防止实例复用时序号不连续
- **MEDIUM**: `compute_recommended_threshold` 改为线性插值分位数,修复 P15 舍入误差 (`threshold_learning.py`)
- **MEDIUM**: `_fetch_window_danmaku_texts` 改为 `if content is not None` 不过滤空串弹幕 (`highlight.py`)
- **MEDIUM**: `_grab_cover` 调用包装 try/except,封面失败不影响切片产出 (`clipper.py`)
- **MEDIUM**: `dashboard_state` 改用 `COUNT(*)` 代替 `.all()` + `len()`,`pipeline_progress` 移除冗余字符串比较 (`service.py`)
- **LOW**: 数据库迁移异常改为 `logger.warning` 记录,权重默认值添加归一化说明 (`session.py`, `scoring_config.py`)
