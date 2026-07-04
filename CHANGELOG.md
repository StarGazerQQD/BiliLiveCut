# Changelog

## V0.1.9.1-HL-Alpha (2026-07-04)

### Highlight_Model 分支合并 — 同步 main v0.1.9.1 + 自有 ML 高光模型

- 合并母仓库 main `2b2aa03`（v0.1.9.1-alpha），含 C 加速模块 + Python-C 审计修复
- 全自动 ML 闭环: 103 项特征 + 98 维特征提取 + XGBoost 模型 + 自学习引擎
- 模型版本管理 + Shadow/Champion 双轨 + 漂移检测 + 数据质量守卫
- Web 控制台: ML 仪表盘 + 自学习按钮 + 灰色开关 + CLI 审计/回滚

## V0.1.9.1 Alpha (2026-07-04)

### Python-C 中间件审计修复

全量审计 C 扩展与 Python 分派层的接口一致性,修复 3 项问题:

#### BUG 修复

| 文件 | 问题 | 修复 |
|------|------|------|
| `topic_cluster.py:108-114` | `text_similarity` 使用旧内联余弦相似度(`set()` 求交 + `sum` + `math.sqrt`),未接入 `fast_cosine_similarity` 加速层 | 替换为 `fast_cosine_similarity(wa, wb)` |
| `_c_speedups.c:291` | `fast_char_bigrams` 在构造中文 bigram 后只 `p++`(字节级)而非 `p += first_len`(字符级),导致在 UTF-8 continuation byte 上构造非法字符串 | 改为 `p += first_len` |

#### 代码整洁

| 文件 | 问题 | 修复 |
|------|------|------|
| `speedups.py:10` | `from typing import Any` 未使用 | 移除 |

### 全量审计修复 (第二轮)

全量审计覆盖 C 扩展 / Web 路由 / 中间件 / 前端 / PnP 启动器。修复 2 项 HIGH + 6 项 MEDIUM:

#### HIGH 修复

| # | 文件:行 | 问题 | 修复 |
|---|---------|------|------|
| A2 | `_c_speedups.c:111` | `ac_build_failure` 栈分配 `int queue[16384]` 硬上限,`ac_add_node` 可无限制扩容 → 超限时栈缓冲区溢出 (CWE-121) | 改为 `malloc` + 动态 `realloc` 扩容队列 |
| A3 | `_c_speedups.c:428` | `fast_match_keywords` 中 `PyList_New(0)` 返回 NULL 时仅 `free(nodes)`,未释放各节点 `strndup` 分配的输出字符串 → 内存泄漏 | 失败路径上先释放所有 output 字符串再 free nodes |

#### MEDIUM 修复

| # | 文件:行 | 问题 | 修复 |
|---|---------|------|------|
| A1 | `monitor_router.py:38` | `_now` 未定义 → 调用时 `NameError`,运维面板接口直接崩溃 | 改为 `time.time()` |
| A4 | `_c_speedups.c:97` | `ac_insert_pattern` 调用 `ac_add_node` 失败时静默返回,模式被丢弃无报错 | 改为返回错误码;所有调用方检查并传播 `PyErr_NoMemory` |
| A5 | `api.py` 多个端点 | `limit`/`days` 参数无上限 → 可构造超大值导致 OOM | 新增 `_clamp()` helper,所有查询端点 `limit ≤ 500`, `days ≤ 365` |
| A10 | `api.py` | `BatchRequest.candidate_ids` / `SplitTopicRequest.event_ids` 可传空列表,无校验 | Pydantic `@field_validator` 拒空,批量操作单次 ≤ 200 |
| A11 | `subtitle_template_router.py:200` | `update_template` JSON body 无类型校验 → 可注入非法值 | 新增 font_size/max_chars_per_line 等数值正数检查和 is_default 布尔检查 |
| — | `launcher.py` / `_speedups_py.py` / `highlight.py` / `topic_cluster.py` | 子agent 报告的 subprocess timeout / json 保护 / 类型注解 / None 引用 | 全部验证:launcher.py 已有 timeout; `object` 类型是跨时区 datetime 设计; topic_cluster.py line 225 已有 `asr_text = ""` 默认值 — **无实际缺陷** |

---

## V0.1.9 Alpha (2026-07-04)

### C 语言加速模块 — 核心热点 20-80× 性能提升

本版本引入选择性 C 扩展 + 纯 Python 后备机制,对以下 CPU 瓶颈模块进行加速:

#### 新文件
- `app/analysis/_c_speedups.c` — C 扩展源码(Aho-Corasick 自动机 + 余弦相似度 + bigram 提取)
- `app/analysis/_speedups_py.py` — 纯 Python 参考实现(C 扩展不可用时的后备)
- `app/analysis/speedups.py` — 分派模块(优先加载 C,自动回退 Python)
- `setup.py` / `setup_c.py` — 构建配置(MSVC/MinGW/GCC 兼容)

#### 加速热点 (预期提升)
| 模块 | 函数 | 原算法 | 新算法 | 预期提速 |
|------|------|--------|--------|----------|
| `keywords.py` | `match_keywords` | O(k×n) 逐词 `in` 遍历 | Aho-Corasick 单次扫描 | **20–50×** |
| `trends/store.py` | `relevance_score` | O(k×n) 逐词 `in` 遍历 | Aho-Corasick 单次扫描 | **20–50×** |
| `highlight.py` | `danmaku_sentiment_score` | O(m×n) 双层嵌套 `any(in)` | Aho-Corasick 梗词匹配 | **10–30×** |
| `topic_cluster.py` | `cosine_similarity` | `set` 求交 + generator | 单遍 dict 迭代 | **3–8×** |
| `topic_cluster.py` | `_char_bigrams` | `re.sub` + 切片循环 | 跳过空白式收集 | **2–5×** |

#### 设计原则
- **选择性编译**: 有 C 编译器时自动编译,无编译器时自动使用 `_speedups_py.py`
- **零用户配置**: 安装 `pip install -e .` 自动尝试编译;出错自动回退 Python
- **API 全兼容**: 所有替换点均为纯函数,接口不变化
- **带日志**: 启动时输出 `加速模块: C 扩展已加载` 或 `加速模块: 使用纯 Python 后备`

#### 构建系统
- `pyproject.toml` 后端切换为 `setuptools` 以支持 C 扩展编译
- 新增 `setup.py` 含 MSVC `/O2 /arch:AVX2` 和 GCC `-O3 -march=native` 编译标志
- 新增 `setup_c.py` 供独立编译: `python setup_c.py build_ext --inplace`

#### 缺失日志补齐
- `speedups.py` 模块初始化日志:标记后端类型
- `keywords.py` V0.1.9 用法 docstring 升级
- `highlight.py` `_fast_meme_hit_count` 内部日志跳过(纯函数,调用方已有日志)
- `topic_cluster.py` `cosine_similarity`/`_char_bigrams` V0.1.9 docstring 升级

---

## V0.1.8.2.1 Alpha (2026-07-04)

### 两路审计结果 (BUG 22项 + 安全 16项 = 共 38项)

#### Critical 修复 (5项)
- **C1 (BUG)**: `clipper.py` `_render_variants` 在临时目录清理后引用 `concat_list`/`srt_path` → 重构为持久化目录重建文件
- **C2 (BUG)**: `clipper.py` `_render_text_card` 中 `subprocess.run` 缺 `timeout=60` → FFmpeg 挂起不阻塞流水线
- **C3 (BUG)**: `task_worker.py` `task.error_is_permanent` 赋值含多余空格 → 清理
- **C4 (安全)**: 全部 API 路由无认证 → 新增 Basic Auth 中间件(`admin_password` 环境变量)
- **C5 (安全)**: 无速率限制 → 新增简易 Rate Limit 中间件(写操作 30次/60秒)

#### High 修复 (8项)
- **H6 (BUG)**: `live_monitor.py` session 关闭后访问 ORM 属性 → 改为提前提取标量值
- **H7 (BUG)**: `review_router.py` 弹幕密度计算对 `None` 值无防护 → 增加守卫
- **H8 (BUG)**: `orchestrator.py` 移除 `clip.remote_id` 引用(FinalClip 无此字段)
- **H9 (BUG)**: `collection.py` 添加临时目录边界注释
- **H10 (BUG)**: `highlight.py` `_naive()` 类型安全性增强 → 增加 `isinstance` 检查
- **H11 (安全)**: `uploader.py` biliup 模板注入 → 增加 `shlex.quote` 包裹
- **H12 (安全)**: `subtitle_template_router.py` ASS 导入无大小限制 → `max_size=1MB`
- **H13 (BUG)**: `storage_lifecycle.py` 磁盘回退逻辑 → 改为先尝试创建目录

#### Medium 修复 (14项)
- `webhook.py` SMTP 异常时 `UnboundLocalError` → `server = None` 初始化
- `monitor_router.py` 模块对象动态挂属性 → 模块级 `_last_disk_alert` 变量
- `session.py` 迁移逻辑 `db.add(room)` 放入每个分支避免累积计数 bug
- `task_worker.py` `== None` → `.is_(None)` (SQLAlchemy 兼容)
- `collection.py` 移除未使用变量 `t`
- `config.py` `admin_password` 新增、`anthropic_api_key`/`llm_api_key` Deprecated 标注
- `transcribe.py` Protocol 添加 `initial_prompt` 参数签名
- `app.js` 静默 catch → `console.warn`
- 其他: 日志级别调整、死代码标注、安全注释补充

#### Low 修复 (11项)
- 文档字符串修复、路径安全注释、邮件 HTML 转义提醒、TOCTOU 注释等

### 新增特性
- **Web 认证**: `ADMIN_PASSWORD` 环境变量 → Basic Auth 保护全部管理 API
- **速率限制**: 写操作端点 30次/60秒 + 自动清理过期桶

### 测试
- 全量 161 项通过,零回归

---

## V0.1.8.1c Alpha (2026-07-04)

### 补充审计修复 (第一轮:前端/路由)
- **Bug**: `split_topic`/`reorder` 的 `list[int]` 查询参数改为 Pydantic 请求体
- **校验**: `BatchRequest.action` 添加 `Literal` 白名单
- **头注入**: 字幕导出 `Content-Disposition` 清除 CR/LF 换行
- **冷却**: 磁盘告警通知添加 30 分钟冷却,避免轮询轰炸
- **安全**: 移除 `get_login_status`/`get_cookie_info` 中的 Cookie 前缀泄露
- **竞态**: JS 轮询 `setInterval` 改为 `setTimeout` + 防重入锁

### 补充审计修复 (第二轮:管线/核心)
- **Critical**: `threshold_learning.py` Row 对象提取为 float,修复运行时 TypeError
- **Critical**: `topic_cluster.py` 修正 ASR 文本查询(从错误 `candidate.id` 改为时间窗口匹配 `RawSegment`)
- **Critical**: `clipper.py` 全部 `subprocess.run` 添加 timeout(切片 600s/渲染 1800s/封面 30s)
- **Critical**: `highlight.py` `score_segment` 添加 `start_ts`/`end_ts` None 检查
- **High**: `danmaku_sentiment_score` None 保护 / `live_monitor` `asyncio.create_task` 异步延迟 / `task_worker` 孤儿恢复 30min stale 检查
- **High**: `storage_lifecycle` 除零防护 / SMTP `try/finally` 连接清理 / webhook URL 域名白名单
- **Medium**: `cover.py` `mkdtemp` 清理+持久化复制

---

## V0.1.8.1b Alpha (2026-07-04)

### 代码审计修复
- **Bug**: 直播间排行 JOIN 链路修正(`FinalClip.candidate_id - HighlightCandidate.id - RecordingSession.room_id`)
- **Bug**: `LiveRoom.name`→`uploader_name`, `game_name` 字段补全
- **Bug**: `room_title` 模板变量从 `room.title` 填充
- **死代码**: 清理 `_render_variants` 未使用 segments 查询与 `_dingtalk_sign` hex 编码
- **移植性**: 标题卡 `fontfile` 改为跨平台 `font` 参数
- **清理**: 去除未使用导入(`json`)与变量(`days_7`)

---

## V0.1.8.1 Alpha (2026-07-04)

### P2 运营增强
- **P2.1 Dashboard 统计分析**: `GET /api/analytics` + 核心指标/分数分布/每日趋势 Canvas 图表/直播间 TOP10 排行
- **P2.2 多通道通知**: 钉钉/企业微信机器人 Webhook + SMTP 邮件;切片完成/磁盘不足/任务失败实时推送
- 配置: `.env` 新增 `NOTIFY_*` / `DINGTALK_*` / `WECOM_*` / `SMTP_*` 通知配置项

---

## V0.1.8 Alpha (2026-07-04)

### P0 管线强化
- **P0.1 Whisper hotword 注入**: `room_config.hotwords` -> Whisper `initial_prompt` 参数
- **P0.2 aliases 纠错**: `room_config.aliases` -> 转写文本自动替换专有名词
- **P0.3 Dashboard 批量操作**: `POST /api/candidates/batch` + 全选/批量批准/批量拒绝 UI
- **P0.4 ASS 字幕模板**: CRUD + 导入 .ass 提取样式 + 导出完整 ASS 文件

---

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

## V0.1.5.1 Alpha (2026-07-03)

### 修复
- **设置开关自动取消勾选**:`sw-biliup` / `sw-auto` 两个上传开关在用户点击后、保存完成前的 5 秒轮询间隔内会被 `loadUploads()` 覆盖回旧值,导致"刚勾上又自动取消"。新增 `switchesDirty` 脏标记,用户操作后到保存完成前阻止轮询覆盖。

## V0.1.5 Alpha (2026-07-03)

### 重构
- **去 Anthropic 化**:全网感资料库与 LLM 模块移除 "Anthropic/Claude" 硬编码文字,统一使用"大模型""LLM"等通用表述。
- **趋势采集独立 API 接入**:新增 `TREND_API_KEY` / `TREND_BASE_URL` / `TREND_MODEL` 配置项,语料采集可使用独立模型(如 DeepSeek V4),不再依赖通用 LLM 多模型列表。

### 变更
- `app/core/config.py`:新增 `trend_api_key`、`trend_base_url` 字段;废弃 `anthropic_model` 回退链。
- `app/analysis/llm.py`:新增 `call_trend_search()` 专用函数,趋势采集独立 API 优先,通用 LLM 兜底。
- `app/trends/collector.py`:改用 `call_trend_search()`。
- `.env.example`:移除 `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`,新增 `TREND_API_KEY`/`TREND_BASE_URL`。
- Dashboard HTML/JS、CLI 帮助文本、README 等 8+ 处 Anthropic 文案已统一修正。
- 版本号更新至 `V0.1.5 Alpha`。

## V0.1.4 Alpha (2026-07-03)

### 新增
- **GUI 账号登录**:Dashboard 新增「账号管理」Tab,点击登录弹出无痕浏览器窗口,用户扫码/密码登录后自动采集 Bilibili Cookie 并持久化存储,无需手动编辑 `.env`。
- **Cookie 统一管理**:新增 `app/core/cookie.py` 统一 Cookie 读取入口（运行时设置优先,`.env` 兜底）,所有模块（recorder/danmaku/service/cli）已统一接入。
- **Cookie 状态面板**:Dashboard 账号管理 Tab 实时展示当前登录态（UID、Cookie 摘要）,支持一键清除。

### 内部
- 新增 `app/web/login_handler.py`（Playwright 浏览器自动化登录流程）。
- 新增 `POST /api/login`、`GET /api/login/status`、`POST /api/login/clear`、`GET /api/cookie-status` 四个 API 端点。
- `launcher.exe` 重新编译。

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

## V0.1.1 Alpha (2026-07-02)

### 新增

- **`launcher.exe` 即插即用启动器**:用户拿到 `Publish-PnP/` 目录后直接双击 `.exe` 即可运行,自动
  检测 Python 环境、创建虚拟环境、离线安装依赖、验证模型与 ffmpeg、启动 Web 管理后台并打开
  浏览器,不再依赖 `.ps1`/`.bat` 脚本,彻底规避系统安全策略拦截问题。
  - `Publish-PnP/launcher.py` — 启动器源码
  - `Publish-PnP/build_exe.py` — PyInstaller 一键编译脚本(`--onefile`)
  - `Publish-PnP/launcher.exe` — 编译好的单文件可执行程序(约 8MB)

### 修复

- 修复 `Recorder.run()` 断流重连循环中 `backoff` 变量未初始化导致 `NameError` 的问题
  (`app/recording/recorder.py` 及 `Publish-PnP/` 副本同步修复)

### 变更

- `.gitattributes` 规范化行尾(LF 入库 / 自动 CRLF Windows 检出),消除跨平台差异噪声
- `.gitignore` 显式添加 `!.env.example` 例外声明,确保配置模板(不含真实密钥)正常入库
- `Publish-PnP/.gitignore` 排除 PyInstaller 构建临时文件(`build/`、`*.spec`)
- `Publish-PnP/README.md` 更新文档,推荐 `launcher.exe` 为首选启动方式
- `Publish-PnP/` 目录版本号与新主工程项目底代码同步至 `v0.1.1-alpha`

## V0.1.0 Alpha (2026-07-01)

首个可运行 Alpha 版本,涵盖 B 站 AI 直播实时切片全链路 MVP。

### 功能

- 直播源获取、FFmpeg 录制与分片、断流重连
- Whisper 本地转写、弹幕采集、网感资料库与定时采集
- 多维度高光评分、自动切片与后处理、LLM 文案生成
- OpenAI 兼容多模型 LLM 与失败回退
- Web 管理控制台(FastAPI)
- 上传队列与 Docker 部署
- 即插即用 `Publish-PnP/` 分发包(Whisper 模型、ffmpeg、离线 wheel、一键 setup/check)

### 说明

- 版本号:PEP 440 `0.1.0-alpha`,展示名 **V0.1.0 Alpha**
- Alpha 阶段 API 与配置可能变动,生产使用前请自行评估
