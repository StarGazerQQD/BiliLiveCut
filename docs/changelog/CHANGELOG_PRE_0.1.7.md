# Changelog — 0.1.7 系列 (已归档)

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

## V0.1.7.2 Alpha (2026-07-04)

### 半成品清理 (审计 F3/F4/F6/F1)
- **F3**: `_decide_status` 迁移到 `auto_approve` + `auto_approve_threshold` 新开关,废弃旧 `mode`
- **F4**: `HighlightTopic` 新增 `chapter_title` 字段,合集章节标题持久化到数据库
- **F6**: `HighlightEvent.asr_text` 创建时自动从 `Transcript` 填充
- **F1**: `produce_clip` 末尾自动创建 `ClipVariant` 多版本记录(SINGLE+按字幕标记)

### 文档清理
- 删除所有 YouTube 相关描述/代码/测试断言
- `collection_copywriter` LLM prompt 和回退输出统一为单一 `title` 字段
- README pip 源顺序统一为阿里云优先+清华备用

---

## V0.1.7.1 Alpha (2026-07-04)

### 安全修复
- **FFmpeg 命令注入防护**:章节标题卡改用 textfile 方式传递文本,消除 drawtext 参数注入风险。
- **路径遍历防护**:`clip_video`/`clip_cover`/`waveform` 端点增加路径验证,确保只返回 clips 目录内文件。
- **XSS 防护**:`review.html`/`collection.html` 模板中 `candidate_id`/`topic_id` 强制转为整数,防止 JS 注入。
- **代码质量**:清除 `review_router.py` 中 9 处 `__import__("sqlmodel")` 反模式为顶部 import;`topics/merge` POST 使用 Pydantic 模型验证参数;`topics/{id}` PATCH 增加字段白名单。

### Publish-PnP 同步
- 同步所有 v0.1.6–v0.1.7 新增/变更文件到 `Publish-PnP/app/`(task_worker, live_monitor, storage_lifecycle, collection, topic_cluster, room_config, review_router, collection_router, monitor_router, collection_copywriter, cover 等)。

---

## V0.1.7 Alpha (2026-07-03)

### P1 补齐
- **音频波形**:FFmpeg PCM 采样 + Canvas 柱状图渲染,播放头三角同步,入/出点绿色区间标注。
- **字幕时间轴**:词级时间戳渲染,按 2.5s 分窗,点击跳转,播放同步高亮行/词。

### P2 合集编辑 + 渲染
- **合集编辑器**:`/collection/{topic_id}` 拖拽排序、时长汇总、相邻事件重叠/间隙检测(≤2s 可合并)、章节标题编辑、文案生成。
- **合集成片**:FFmpeg concat 多片段拼接,EBU R128 响度标准化,可选章节标题卡(纯色背景+白色文字,2s)。
- **文案生成**:LLM 辅助 + 规则回退双路。输出:主题摘要、标题、简介、章节时间戳、封面短标题、标签。
- **房间级配置**:热词/别名/高光关键词/屏蔽话题,存储于 `LiveRoom.room_config_json`,Dashboard 折叠面板编辑。

### 新增文件
- `app/pipeline/collection.py` — 合集渲染 + 重叠检测
- `app/web/routers/collection_router.py` — 合集编辑 API
- `app/web/templates/collection.html` — 合集编辑器页面
- `app/publishing/collection_copywriter.py` — 合集文案生成
- `app/analysis/room_config.py` — 房间配置工具

### 测试
- 新增 27 项 P1/P2 单元测试,全量 149 项通过,零回归。

---
