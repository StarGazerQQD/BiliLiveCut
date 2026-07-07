# Changelog — 0.1.9 系列 (已归档)

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

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
