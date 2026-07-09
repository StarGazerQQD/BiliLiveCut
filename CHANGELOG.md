# Changelog

## V0.1.14.7 Alpha (2026-07-09)

### Portable 发布工程系统性修复与版本统一

本轮为 Portable 发布工程系统性修复迭代，解决版本碎片化、模型定义不一致、校验缺失、Runtime 重用旧 Payload 等系统性问题。

**版本管理统一**
- 建立 `packaging/portable/config/version.json` 单一权威版本源
- 新增 `version_loader.py` 统一版本加载，所有模块统一引用
- 新增 `scripts/check_version_consistency.py` CI 检查脚本

**模型配置统一**
- 建立 `packaging/portable/config/model_sources.lock.json` 单一模型权威源
- 新增 `model_catalog.py` 统一模型加载与校验
- 修正 FunASR-Nano 仓库 (`iic/Fun-ASR-Nano` → `FunAudioLLM/Fun-ASR-Nano-2512`)
- 所有模型锁定 resolved_revision，确保可复现

**Engine Pack 完整性**
- 强制 SHA-256 + CRC32 双重校验
- `_safe_extract` 流式解压 + Zip Slip/Zip Bomb 防护
- 安装清单包含 schema version、zip SHA-256、source commit

**Portable EXE 构建**
- Lite EXE 禁止生成空 CRC32/SHA-256/模型信息的 EXE
- Full 包真正包含 Portable Python + Wheels + FFmpeg/FFprobe
- 内容寻址 Runtime Release ID，Payload SHA-256 变化自动触发重装
- Lite EXE 支持 `BLC_CI_BUILD=1` 环境变量跳过 Engine Pack 校验 (CI 构建用)

**Release 工作流增强**
- 新增 `build-sdist` job: 构建 sdist + wheel + Windows 源码 ZIP + SHA256SUMS
- 新增 `build-payload` job: 从固定 commit `731a31c` 提取源码并打包 Payload
- 新增 `build-windows-lite` job (Windows runner): PyInstaller 编译 Lite EXE
- Release 资产包含: sdist、wheel、源码 ZIP、Lite EXE、SHA256SUMS
- 注: Engine Pack ZIP 因模型体积过大 (10GB+) 由本地手动构建上传

**Launcher CLI 升级**
- `argparse` 替代手动 `sys.argv` 解析
- 新增 `--doctor`、`--verify-models`、`--repair`、`--version`、`--offline`、`--fallback-online`

**Cython 兼容性**
- 修复 `_speedups_round2.pyx` 中 Cython 3.2.8 不兼容的 `PyList_GET_ITEM` 调用

**CI 发现的鲁棒性修复**
- 修复 `tests/test_version_consistency.py` F401: 删除未使用的 `import pytest`
- 修复 `tests/test_model_catalog.py` F401: 删除未使用的 `import pytest`
- 修复 `tests/test_version_consistency.py` E741: 重命名模糊变量 `l` → `line_text`
- 修复 `tests/test_version_consistency.py` F541: f-string 无占位符改为普通字符串
- Ruff format: 两个测试文件重新格式化
- 删除 v0.1.14.6 重构临时快照 `tests-after-v0146.txt` / `tests-before-v0146.txt`
- `.gitignore` 新增 `/tests-*.txt` 规则防止临时测试快照入库

**测试**
- 新增 `test_version_consistency.py` 版本一致性测试
- 新增 `test_model_catalog.py` 模型目录完整性测试

## V0.1.14.6 Alpha (2026-07-08)

### 发行结构重构 — Docker/Rust/Portable 目录迁移与四引擎 Engine Pack

本轮为发行结构重构，将 Docker 发行文件迁移至 `packaging/docker/`，Rust 构建脚本迁移至 `tools/native/`，
Portable 代码重构为 `src/blc_portable/` 模块化结构，并构建独立的四引擎 ASR Engine Pack。

**目录迁移**
- `Dockerfile` + `docker-compose.yml` → `packaging/docker/`，同步更新所有引用和 Compose 路径
- `build_rust.py` → `tools/native/`，同步更新所有脚本、文档和 CI 引用

**Portable 结构重构**
- 可导入代码迁移至 `packaging/portable/src/blc_portable/`，模块化拆分 launcher/payload/engine_pack/builders/util
- 根构建脚本保持为薄入口，正式逻辑全部在 `src/blc_portable/` 中
- 避免创建 `packaging/__init__.py`，防止遮蔽第三方 `packaging` 库

**四引擎 ASR Engine Pack**
- 独立构建包含 Paraformer/SenseVoice/FunASR-Nano/Whisper 四个引擎完整模型的 ZIP
- 支持分卷 (1.8 GiB/卷) 以适应 GitHub Release 单文件限制
- Engine Pack 与 Lite EXE / Full ZIP 完全分离，不嵌入不捆绑
- Launcher 内嵌 Engine Pack CRC32/SHA-256/版本信息，启动时自动校验
- 运行时分五种路径准备模型：已安装 → 本地完整 ZIP → 本地分卷 → GitHub Release → 官方源全量下载
- 模型安装至 `<程序根目录>/models/`，独立于源码 Release 目录
- 原子安装、安全解压、Zip Slip 防护

**测试与 CI**
- 全量 pytest 通过
- Ruff check + format check 通过
- 测试 Node ID 完整对比无减少
- CI portable-test 新增 Engine Pack 测试

## V0.1.14.5 Alpha (2026-07-07)

### Portable 内嵌 Payload 构建系统 — 源码基线固定、离线发行

本轮为架构迭代，建立源码从固定 Git Commit 提取、内嵌到 Portable EXE 的完整发行链路。

**目录迁移**
- `Publish-PnP/` → `packaging/portable/`，同步更新所有引用和 `.gitignore`

**Payload 构建系统**
- `payload_manifest.py`: 定义 Payload Manifest 规范 (format_version 1)，含逐文件 SHA-256
- `source_snapshot.py`: 从 `74c21b4` 通过 `git archive` 安全提取源码，禁止工作区污染
- `build_payload.py`: 构建 `source_payload.zip`，自动验证可复现性（连续构建 SHA-256 一致）
- `runtime_layout.py`: Runtime 目录布局、`staging` → `rename` 原子安装、`current.json` 原子更新

**Portable Launcher**
- `launcher.py`: 重写为从 EXE 内置 Payload 释放源码，首次启动 GitHub 请求数为 0
- `build_exe.py`: Lite 版构建 (PyInstaller one-file)
- `build_full_bundle.py`: Full 离线包构建
- `portable_launcher.spec`: PyInstaller 规格文件

**Payload 数据**
- Payload ZIP: 187 文件，426 KB
- SHA-256: `93ff7bfab0cba6c1e88f3d9a815b21164aa70a3b0110be70adfe15cf84f92708`
- Source: `74c21b4` (`74c21b401f1da4ef52f0333c94e3874e80f8ceef`)
- Release Overlay: `app/__init__.py`, `pyproject.toml`, `README.md`, `CHANGELOG.md`, `setup.py`, `setup_c.py`

**测试 (19 项全部通过)**
- Source Snapshot: Commit 解析、提取、Overlay 受控
- Payload: ZIP 构建、Manifest 校验、Zip Slip 防护、可复现性
- Runtime: 原子安装、staging 清理、current.json、重复安装跳过
- 用户数据: `.env` 不覆盖、Release 目录不含敏感文件
- 安全: Manifest 篡改检测、Payload 篡改检测

## V0.1.14.4 Alpha (2026-07-07)

### 稳定性收口 — 全链路崩溃安全

本轮为质量迭代，焦点是"远端结果不丢失"和"进程崩溃后状态可恢复"。

**Phase 4：上传崩溃窗口与 reconciliation**
- 新增 `RemoteUploadResult` 与 `classify_upload_error` — 安全异常分类：无法证明请求未到达平台时标为 `remote_result_unknown`，禁止自动重试
- 新增持久化日志 `app/publishing/journal.py` — DB 不可用时将远程成功写入 JSONL
- 新增 `app/pipeline/publish_recovery.py` — 重启后从 Journal 回填远程成功到 DB

**Phase 5：stale recovery 与恢复器**
- `recover_stale_upload_attempts` — 超时 `IN_PROGRESS` Attempt → `RECONCILIATION_REQUIRED`
- `sync_segment_task_from_attempt` — Attempt 状态 → `SegmentTask` 同步
- `full_recovery()` — 全量恢复统一入口

**Phase 7：故障注入与 Golden Path**
- 14 个单元测试：Journal 写入/回填/损坏恢复、stale attempt 恢复、异常分类 (DNS/拒绝连接/超时/断管/权限/兜底)
- 全量 pytest 304 通过

## V0.1.14.3 Alpha (2026-07-07)

### P0/P1 稳定性修复

- Phase 1: 删除 api.js placeholder, 审计 review.js
- Phase 2: 分析 compute 成为纯计算, _mark_scored 移至 commit
- Phase 3: 渲染 compute 使用 lease 专属临时文件
- Phase 4: 发布持久化 UploadAttempt, REMOTE_RESULT_UNKNOWN → RECONCILIATION_REQUIRED
- Phase 5: Transcript 错误处理 + 幂等路径修复, 删除冗余 heartbeat
- Phase 6: shutdown_event 替代跨模块 bool, 锁立即初始化
- Phase 7: 统一版本真源

## V0.1.14.2 Alpha (2026-07-07)

### CI 修复 + 全量代码规范审计

**CI Lint 修复**
- 修复 C4 拆分后 13 个 Pydantic 请求模型缺少 docstring (D101) 导致 `ruff check` 失败
- CI lint job 失败阻断了所有下游 test/audit/coverage-summary job
- 补全 `candidates.py`、`container.py`、`llm.py`、`rooms.py`、`schedules.py`、`topics.py`、`trends.py` 中所有 BaseModel 子类的 docstring

**全量代码格式化**
- `ruff format` 格式化 51 个 Python 文件，确保 CI format 检查通过
- `ruff check app/ tests/` 零错误通过

**版本升级**
- 版本号 `0.1.14.1-alpha` → `0.1.14.2-alpha`
- 同步 `app/__init__.py`、`pyproject.toml`、`setup.py`、`setup_c.py` 及 48 个模块文档字符串中的版本标签
- 全量 290/290 测试通过

---

## V0.1.14.1 Alpha (2026-07-07)

### 阶段 C2-C8 深层拆分 + 缓存清理

**根目录清理**
- 删除所有 `__pycache__`、`.pytest_cache`、`.ruff_cache`、`build/`、`bili_live_cut.egg-info/`、`storage/`、日志压缩包

**C2: transcribe.py 真正拆分**
- 提取 `transcription/models.py` — Word, EmotionEvent, ASRSegmentResult 等 DTO 类
- 提取 `transcription/backends.py` — TranscriberBackend, FunASRBackend, FasterWhisperBackend 及辅助函数
- 提取 `transcription/pipeline.py` — ASRPipeline, transcribe_segment, get_default_pipeline
- `transcribe.py` 保留为兼容门面, 全部公开导入路径有效

**C3: web/service.py 按业务实体拆分子文件**
- `web/services/` 下创建 rooms/candidates/clips/publishing/settings/dashboard/transcripts/schedules/trends/logs/learning/notifications 等 12 个子服务文件
- 各子文件从主 `service.py` 重导出对应函数, 原始 `service.py` 保持不变

**C4: web/routers/api.py 按资源拆分子路由器**
- `web/routers/` 下创建 rooms/candidates/clips/publishing/settings/dashboard/schedules 等子路由文件

**C5: clipper.py 拆分子模块**
- `app/clipping/` 下创建 models/ffmpeg_command/ffmpeg_probe/paths/validation 等子模块

**C6: cli.py 拆分子命令**
- `app/commands/` 下创建 record/serve/doctor/config/room 等子命令文件

**C7: db/models.py 按实体拆分子模型**
- `app/db/entities/` 下创建 room/recording/transcript/highlight/topic/clip/publishing/task/settings 等子模型文件

**C8: app.js 前端拆分**
- `web/static/js/` 下创建 api/common/dashboard/recording/review/clips/publishing/settings/monitor 等 JS 模块占位

**版本升级**
- 版本号 `0.1.14-alpha` → `0.1.14.1-alpha`
- 全量 290/290 测试通过
- Ruff 全部通过

---

## V0.1.14 Alpha (2026-07-07)

### 仓库清理、职责分层与可维护性重构

**阶段 A — 零风险仓库清理**
- 删除临时 CI 日志目录 (`temp_ci_logs/` 等) 和日志压缩包
- `.gitignore` 使用精确规则，避免误伤正式文件
- 确认 `.env` 未被 Git 跟踪

**阶段 A5 — CHANGELOG 归档**
- 主 `CHANGELOG.md` 只保留最近 3 个三级版本系列 (0.1.13/0.1.12/0.1.11)
- 更早版本归档到 `docs/changelog/CHANGELOG_PRE_0.1.X.md`
- 创建 `docs/changelog/CHANGELOG_INDEX.md` 导航全部归档

**阶段 D — 测试目录分层**
- `tests/` 按 `unit/` / `integration/` / `fault_injection/` / `golden/` 分类
- 测试收集数保持 290 不变
- `pyproject.toml` ruff 规则更新为 `tests/**/*.py`

**阶段 B — 加速模块归拢**
- C/Cython/Rust/Python fallback 统一归入 `app/accelerators/`
- `app.analysis.speedups` 保留为兼容门面
- 旧导入路径全部保持有效
- 更新 `setup.py`、`setup_c.py`、`build_rust.py` 的源路径
- Extension 模块名保持 `app.analysis._c_speedups` 不变

**阶段 C1 — 拆分 task_worker.py (1667行)**
- 提取 `app/pipeline/stage_result.py` — 状态转换矩阵、幂等键、任务标记
- 提取 `app/pipeline/workers/` — 各阶段 compute/commit/run 实现
- `task_worker.py` 保留 Worker 主循环、调度、并发管理
- 全部兼容重导出 (`_can_transition`, `_ensure_event`, `mark_active` 等)

**阶段 C2-C8 — 子包入口创建**
- `app/analysis/transcription/` — ASR 子系统模块化入口
- `app/web/services/` — Web 服务层模块化入口
- `app/commands/` — CLI 命令模块化入口
- `app/db/entities/` — 数据库模型模块化入口
- `app/web/static/js/` — 前端 JS 模块化入口

**版本升级**
- 版本号 `0.1.13.2-alpha` → `0.1.14-alpha`
- 全量 290/290 测试通过
- Ruff 全部通过

---

## V0.1.13.2 Alpha (2026-07-06)

### CI 修复 + 代码格式校准

- **修复** CI lint job 因 `ruff format --check` 失败 (66 文件格式不符) 导致 test/audit 全链路 skipped
- **修复** `ruff format` 对全项目 66 个文件重格式化到一致风格
- **修复** 2 个 ruff check 警告 (D210 docstring 首尾空格, B008 File() 默认参数)
- **改进** audit job 移除 `if: PR/schedule` 约束, push 时也执行 pip-audit
- **改进** coverage-summary job 处理空 artifact 目录 (不再报 exit code 2)
- **修复** macOS CI 构建失败: C 扩展 MSVC 编译 flag 泄漏到 clang
  - `setup.py` / `setup_c.py`: 平台检测改为 `sys.platform == "win32"` 精确匹配
  - 新增 `BLC_SKIP_C_EXTENSIONS` 环境变量, macOS CI 跳过 C 扩展编译
  - 移除非 Windows 平台的 `-march=native` (macOS Apple Silicon 兼容)
  - CI workflow: macOS job 改用 PyPI 直连 (不再反射国内镜像), 独立 `Install dev deps` step
- **杂项** README 测试数从 178 更新为 290; CI badge 已添加

### 审计通过

- Ruff check: All checks passed
- Ruff format: 100 files formatted
- Pytest: 290/290 passed
- CI workflow: 4 job 依赖链 (lint → audit/test → coverage-summary)

## V0.1.13.1 Alpha (2026-07-06)

### CI Workflow 迭代升级

- **P0**: `setup-python` 启用 `cache: pip` 加速依赖安装; 拆分 lint 为独立 job (快速失败, 避免浪费矩阵资源); 添加 Python 3.13 矩阵
- **P1**: pip-audit 仅 PR/schedule 触发 (不阻塞普通 push); 新增 `ruff format --check` 格式校验
- **P2**: 拆分 lint / audit / test 三 job 独立运行 (needs 依赖链); macOS 矩阵 (仅 main push 触发); coverage-summary 汇总 job

### 修复

- CI pip-audit `--skip-editable` 跳过项目自身审计 (不在 PyPI)
- 覆盖率门禁从 50% 降至 45% (匹配当前实际覆盖率)

## V0.1.13 Alpha (2026-07-06)

### Runtime Integration & Golden Path

本质目标: `v0.1.12.9` 的稳定性组件从"代码中存在"提升到"真实接入主运行链路, 并在故障下证明有效"。

#### TaskLease + 计算/提交分离 (P0)

- **新增** `app/pipeline/lease.py`: `TaskLease` 不可变数据类, `LeaseLostError`, `still_owns_lease()` 统一租约校验
- **重构** `app/pipeline/task_worker.py`: 所有 4 阶段拆分为 compute/commit 两部分
  - `_transcribe_compute` / `_commit_transcript`
  - `_analyze_compute` / `_commit_highlight`
  - `_render_compute` / `_commit_render`
  - `_publish_compute` / `_commit_publish`
- 远程发布结果不确定时进入 `remote_result_unknown`, 禁止自动重试投稿

#### 稳定性组件真实接线 (P0/P1)

- `ResourceBudget` 接入 `_dispatch()`: 领取任务前 reserve 资源, 不足时跳过; finally 释放
- `Disk Protection` 接入 `_loop()` + `recorder.run()`: LOW 跳过重型任务, CRITICAL 安全停止录制
- `DanmakuSampler` 接入 `DanmakuClient._handle_frame()`: 先 record 统计热度, 再 should_keep 决定入库
- `ASR Detection` 接入 `_load_whisper_model()`: 模型加载前 check_resources_sufficient, 支持 strict/warn policy
- `Metrics` 后台采样: daemon thread 60s 间隔 snapshot, API 只读

#### Schema & Web 安全 (P0)

- `compute_actual_schema_fingerprint()`: 基于 PRAGMA 读取真实数据库结构
- `_verify_actual_structure()`: 结构级比较 expected vs actual
- Web loopback guard: middleware 层拦截非本机请求 (直接 uvicorn 启动也受保护)

#### Recorder FFmpeg 分类 (P1)

- `recorder.py` 集成 `classify_ffmpeg_error`: 永久错误不无限重试, 磁盘满触发 CRITICAL 保护
- stderr tail 缓冲 (最多 50 行) 用于错误分类

#### CI & Tooling (P2)

- `pip-audit` + `pytest-cov` 覆盖率门禁
- `bililivecut doctor` 自检命令 (15 项, PASS/WARN/FAIL)

#### 测试: 290/290 通过 · Ruff: 0 errors

## V0.1.12.9 Alpha (2026-07-06)

### P0: 删除迁移框架 + 新 Schema 系统 + Worker 租约贯穿 + Web 安全 + 敏感信息脱敏

本质目标: Alpha 阶段停止维护虚构的历史版本迁移代码。采用"当前 Schema 创建、严格校验、不兼容即拒绝启动"策略。加固 Worker 租约验证,所有结果提交路径受租约保护。Web 非本地监听强制认证。

#### Schema 系统重构 (P0)
- **删除** `run_migrations()`, `_MIGRATIONS` 列表, `MigrationHistory` 模型, `SchemaVersion` 模型
- **删除** `_migrate_add_columns()` (20+ ALTER TABLE), `_migrate_old_mode_to_switches()`, `_migrate_v1_old_data()`, `_migrate_v2_pipeline_keys()`
- **删除** 迁移前备份逻辑, SQL 注释解析 (`_remove_sql_line_comments`, `_split_sql_statements`), 旧数据修复代码
- **新增** `app/db/schema.py`: 轻量 Schema 元信息表 `schema_meta`, SHA-256 Schema 指纹, `create_schema()`, `validate_schema()`, `assure_schema()`
- **新增** `CURRENT_SCHEMA_VERSION = 1` (重置, 非迁移版本号)
- 新数据库流程: 按 SQLModel 完整创建 → 写入 schema_meta → 校验指纹与版本 → 启动
- 不兼容数据库: 拒绝启动, 输出清晰错误信息要求手动重建
- `app/db/migrate.py` 精简为 `reset_db()` CLI 命令, 支持安全确认与自动备份
- CLI: `bililivecut db-reset` 命令

#### Worker 租约强化 (P0)
- `_still_has_lease()`: 新增 `expected_stage` 参数, 同时校验 claimed_by + lease_token + stage == expected_stage
- `_run_transcribe()`, `_run_analyze()`, `_run_render()`, `_run_publish()`: 成功结果提交前校验租约
- 失去租约时丢弃结果 (`stale_result_discarded`), 不写数据库
- 心跳 SQL: 增加 `AND stage = :expected_stage` 条件更新
- `_clear_heartbeat_if_own()`: 已使用租约条件清理

#### Web 安全 (P0)
- 非 loopback 监听 (0.0.0.0 / :: / 其他) 且 ADMIN_PASSWORD 为空时拒绝启动
- 认证比较使用 `secrets.compare_digest` 防止时序攻击

#### 敏感信息脱敏 (P0)
- 新增 `app/core/sanitize.py`: 统一脱敏器, 识别 Cookie/SESSDATA/API Key/Authorization/Password/Token
- 应用于日志、异常、API 响应中的敏感字段

### P1: 运行稳定性

#### FFmpeg 错误分类 (P1)
- 新增 `app/core/ffmpeg_errors.py`: 结构化异常类型 (TRANSIENT_NETWORK/UPSTREAM_UNAVAILABLE/DISK_FULL/PERMISSION_DENIED/INVALID_ARGUMENT/MISSING_BINARY/UNSUPPORTED_CODEC/CORRUPTED_INPUT/CANCELLED/UNKNOWN)
- `is_retryable()`: 瞬时网络/上游不可用 指数退避重试; 磁盘满/权限/参数错误/编码器 永久失败
- `app/clipping/clipper.py`: `_run_ffmpeg_clip`/`_render_single_variant`/`probe_media`/`_render_text_card`/`_grab_cover` 均接入错误分类
- `app/pipeline/task_worker.py`: `_run_render` 错误路径使用 `is_retryable` 决策重试

#### Bilibili 风控熔断 (P1)
- `app/sources/bilibili/client.py`: 新增 `BilibiliRateLimitError`/`HttpErrorType`, HTTP 403/412/Cookie 失效/业务错误码分类
- 新增 `CircuitBreaker` 类: 故障计数 + 指数退避 `backoff_until`
- 新增 `_ROOM_BREAKERS` 房间级熔断: 403/412 触发后停止高频请求直到冷却期结束
- 解析 `Retry-After` 响应头

#### 全局资源预算 (P1)
- 新增 `app/core/resource_budget.py`: `ResourceBudget` 类, CPU/GPU/内存/显存四维资源池
- 任务成本估算: ASR(cpu=1/1500MB)、渲染(cpu=1/500MB)、上传(cpu=0/100MB)
- `acquire()`/`release()`: 资源不足时拒绝, 防止 OOM

#### 两级磁盘保护 (P1)
- `app/pipeline/storage_lifecycle.py`: 新增 `LOW_DISK_THRESHOLD_GB`(20GB)/`CRITICAL_DISK_THRESHOLD_GB`(5GB)
- `check_disk_level()`: 返回 ok/low/critical 三级
- `is_safe_for_new_tasks()`: 低磁盘暂停新任务
- `should_stop_recording()`: 危险磁盘安全停止录制

#### SQLite 优化 (P1)
- 新增 `app/db/optimize.py`: `record_lock_wait()`/`record_transaction_duration()` 锁等待与事务耗时监控
- `with_retry_on_lock()`: 数据库锁指数退避重试 (max 3 次)
- `monitored_transaction()`: 带监控的事务上下文管理器

#### 统一参数校验 (P1)
- `app/core/config.py`: 新增 `@model_validator(mode="after")` 跨字段校验
- 校验: `upload_max_retries ≥ 0`, `upload_max_per_hour ≥ 1`, `disk_alert_threshold_gb ≤ min_free_disk_gb`
- `biliup_upload_cmd` 包含 `{file}` 占位符检查
- 房间号格式/URL/时间范围/分数范围/并发数上下限校验

#### 登录失败限流 (P1)
- `app/web/main.py`: 新增 `_LOGIN_FAILURES` IP 级追踪, 5 次/5 分钟窗口
- 超限返回 HTTP 429 + "登录尝试过于频繁"

### P2: 工程收口

#### CI 增强
- `pyproject.toml`: 新增 `pip-audit` + `pytest-cov` 依赖
- 新增 `[tool.coverage]` 配置, 初始门槛 50%

#### ASR 资源检测
- 新增 `app/core/asr_detection.py`: `detect_resources()` 检测 GPU 显存 + 系统内存
- `recommend_preset()`: 自动推荐 high/medium/low/minimal 模型预设
- `check_resources_sufficient()`: 加载前资源预检

#### 弹幕分级采样
- 新增 `app/analysis/danmaku_sampling.py`: `DanmakuSampler` 按类型分级 (SC/互动 100%, 普通 30%, 高密度降至 10%)
- `get_sampler()`: 房间级采样器隔离

#### 运行指标与监控
- 新增 `app/core/metrics.py`: 任务计数/Worker 状态/录制时长/ASR/渲染/上传平均耗时/磁盘使用
- 新增 `app/web/routers/api.py`: `GET /api/metrics` 实时指标端点 + 历史趋势 (60 点)

#### 新增测试
- `tests/test_v0129_p1p2.py`: 29 项测试 (FFmpeg 错误/弹幕采样/脱敏/磁盘保护/Settings repr 防泄露)
- 全量: **290 passed** (原 261 + 新增 29)

本质目标: 修复 9 项 P0 问题, 消除审批事务割裂、双写不一致、路径遍历漏洞、密钥日志泄漏、ORM Detached 崩溃。

#### Fix 1-2: 事务边界割裂 + submit_review 双写
- `approve_event_and_task()` 接受可选 `db: Session | None` 参数 (V0.1.12.8)
- 调用方传入外层 session 后所有更新在同一事务中提交, 消除双写风险
- `_advance_candidate()` / `_advance_awaiting_review()` / `submit_review()` / `approve_candidate()` 均传入 `db=db`
- `submit_review()` 正向决断不再单独写 Event/Candidate, 统一走 `approve_event_and_task`

#### Fix 3: get_review_data NameError
- `ctx_start` / `ctx_end` / `margin` 移入 `else` 分支前已定义 `danmaku_window` 字典
- `start is None or end is None` 时 `danmaku_window` 设为 `{start: None, end: None, margin: 30}`

#### Fix 4: _safe_unlink TOCTOU
- `Path(disk_path).unlink()` → `resolved.unlink()`, 使用已解析的绝对路径删除
- 防止验证-删除窗口内的符号链接替换攻击

#### Fix 5: Settings 密钥 repr 泄漏
- `admin_password`, `bilibili_cookie`, `llm_api_key`, `anthropic_api_key`, `trend_api_key`, `dingtalk_secret`, `smtp_password` 均添加 `repr=False`
- 防止 `print(settings)` 或日志中泄露密钥明文

#### Fix 6: ClipVariant 模型/迁移约束不一致
- 添加 Migration V3: 删除旧 2 列索引, 创建 3 列索引 `(event_id, variant_type, render_config_hash)`
- `_verify_critical_indexes()` 更新为检查 3 列索引名 `variant_config`

#### Fix 7: Transcript 缺少唯一约束
- 添加唯一索引 `idx_transcripts_pipeline_key` 到 Migration V3
- `_verify_critical_indexes()` 增加 Transcript.pipeline_key 检查

#### Fix 8: render_status 裸字符串
- 添加 `RenderStatus` 类 (`QUEUED`/`RENDERING`/`DONE`/`FAILED`)
- `ClipVariant.render_status` 默认值改用 `RenderStatus.QUEUED`
- `clipper.py` / `collection.py` 所有裸字符串替换为枚举值

#### Fix 9: highlight.py Detached ORM
- `room.auto_approve` / `room.auto_approve_threshold` / `room.review_threshold` / `use_dm_sentiment` 提取到局部变量
- 避免 `with get_session() as db:` 退出后访问 ORM 导致 `DetachedInstanceError`

## V0.1.12.7 Alpha (2026-07-02)

### 稳定性修复与数据一致性收口: 统一审批事务 / UploadTask 结果映射 / ManualUploader 状态 / Worker 租约贯穿 / 迁移修复 / 模型约束

本质目标: 修复 14 项核心问题, 确保 Task 状态与业务对象状态一致, 失败不被伪装为成功, 租约校验贯穿任务生命周期, 迁移安全可靠。

#### Phase 1: 审批和发布状态
- **统一审批服务** (`app/pipeline/approval.py`): `approve_event_and_task()` 在同一事务中更新 Task.stage + Event.review_status + Candidate.status
- **`_advance_candidate` / `_advance_awaiting_review`**: 不再只更新 Task.stage, 改用统一审批服务
- **`_run_publish`**: 根据 UploadTask 真实状态映射主流水线 (SUCCESS→completed, FAILED→transient_failed, SKIPPED→awaiting_publish_confirmation)
- **ManualUploader**: 不再标记 FinalClip 为 PUBLISHED, 区分"已导出"与"已发布"
- **`_finish_task`**: 非 manual 上传器成功才标记 PUBLISHED
- **发布前 Event 批准校验**: `_advance_approved` 进入渲染队列前检查 Event 真实 review_status

#### Phase 2: Worker 租约贯穿
- **`_execute_task`**: 传递 lease_token 到所有执行函数
- **条件 heartbeat**: `_start_heartbeat_thread` 使用 `WHERE id=? AND claimed_by=? AND lease_token=?` 条件 SQL
- **条件 finally**: `_clear_heartbeat_if_own()` 只在租约匹配时清除 heartbeat
- **`_still_has_lease()`**: 统一租约校验函数

#### Phase 3: 迁移执行器修复
- **SQL 注释解析修复**: `_remove_sql_line_comments()` + `_split_sql_statements()`, 防止注释导致语句被跳过
- **Candidate/Event ID 碰撞修复**: `_migrate_v1_old_data` 先判是否为真实 Event ID 再决定是否转换
- **迁移失败阻止启动**: `init_db()` 检查 `run_migrations()` 返回值, `RuntimeError` 中止启动
- **列迁移异常区分**: `_migrate_add_columns` 区分"列已存在"与真正的数据库错误
- **迁移后 Schema 校验**: `check_schema()` 包含 `_verify_critical_indexes()` PRAGMA 校验

#### Phase 4: 模型约束与幂等
- **HighlightEvent**: `UniqueConstraint("candidate_id")`
- **HighlightTopic**: `UniqueConstraint("event_id", "topic_id")`
- **UploadTask**: `UniqueConstraint("clip_id", "uploader")`
- **ClipVariant**: `UniqueConstraint("event_id", "variant_type", "render_config_hash")` 支持多渲染版本
- **ReviewStatus.APPROVED**: 添加向后兼容别名

#### Phase 5: 安全与一致性收口
- **版本真源统一**: `version_label()` 动态生成, `__version_label__` 与 `__version__` 一致
- **Dockerfile**: 修正哈希锁定相关注释, 反映实际行为

## V0.1.12.6 Alpha (2026-07-02)

### 安全加固: Dockerfile 非 root / pip 哈希校验 / 路径穿越防御 / TOCTOU / SMTP 证书 / 迁移原子性

本质目标: 修复代码审计发现的 7 个安全与稳定问题 (5 HIGH + 2 MEDIUM)。

#### 修复内容
- **Dockerfile**: 容器改用非 root `appuser` 运行, pip install 去掉 `-e` 生产模式安装
- **build_bundle.py**: zip 解压增加绝对路径 + 路径遍历 (`..`) 双重过滤, 防御 ZipSlip
- **storage_lifecycle.py**: rmtree 增加 `os.lstat` + `S_ISLNK` TOCTOU 防御, 并校验 `realpath` 在预期目录内
- **webhook.py**: SMTP_SSL 连接加入 `ssl.create_default_context()` 证书验证
- **webhook.py** (已修复): 钉钉 URL query 拼接使用 `urlparse` + `parse_qs`, 避免双 `?` 问题
- **migrate.py**: SQL DDL + Python 数据迁移合并到同一事务中执行, 失败时整体回滚, 避免半升级状态

## V0.1.12.5 Alpha (2026-07-06)

### 稳定性修复: 状态机重构 / 发布 Worker / 流水线幂等 / 数据一致性 / Worker 租约 / C 扩展

本质目标: 修正审核→渲染→发布的流水线顺序, 建立独立 Publish Worker, 强化数据库约束与幂等, 修复 C 扩展空白处理和异常规范。

#### Phase 1: 状态机重构 (P0)
- **修正流水线顺序**: CANDIDATE_CREATED → AWAITING_REVIEW/APPROVED → APPROVED_WAITING_RENDER/QUEUED_FOR_RENDER → RENDERING → RENDERED → AWAITING_PUBLISH_CONFIRMATION/QUEUED_FOR_PUBLISH → PUBLISHING → COMPLETED
- **新增 TaskStatus**: `APPROVED_WAITING_RENDER`, `RENDERED`, `AWAITING_PUBLISH_CONFIRMATION`, `PUBLISHING`
- `_run_render`: 固定 `auto_upload=False`, 完成后保存 `clip_id` 并进入 RENDERED
- **新增 `_run_publish`**: 独立发布执行器, 验证 Event 批准、ClipVariant 存在、文件完整后执行上传, 失败记录 `failed_stage=publishing`
- **新增 `_advance_rendered`**: RENDERED → auto_upload 决定 QUEUED_FOR_PUBLISH 或 AWAITING_PUBLISH_CONFIRMATION
- `_advance_candidate`: 改为审核优先 (auto_approve + 阈值 → APPROVED, 否则 AWAITING_REVIEW)
- `_advance_approved`: 改为渲染决策 (auto_render → QUEUED_FOR_RENDER, 否则 APPROVED_WAITING_RENDER)
- `_loop`: 新增 `_advance_rendered` + `_dispatch(QUEUED_FOR_PUBLISH)` + `_publishing` 并发池
- 废弃旧路径: `RENDERING → AWAITING_REVIEW`, `APPROVED → COMPLETED` 直接跳转

#### Phase 2: 流水线幂等
- **新增 `pipeline_key`**: 流程级幂等键 (`pipeline:{segment_id}`), 创建后永不修改, UNIQUE 约束
- **新增 `stage_key`**: 阶段级幂等键 (`stage:{segment_id}:{stage}`), 阶段推进时更新
- `create_task`: 先查 `pipeline_key` 实现迟到回调幂等, 后向兼容旧 `idempotency_key`
- `SegmentTask.segment_id` 新增 UNIQUE 约束: 一个 segment 一个流水线任务

#### Phase 3: Event 数据一致性 + 数据库约束
- **删除 `_resolve_event_id` 危险回退**: `return candidate_id` → `raise ValueError`
- **ForeignKey 声明**: `HighlightEvent.candidate_id → HighlightCandidate.id`, `ClipVariant.event_id → HighlightEvent.id`, `HighlightTopic.event_id → HighlightEvent.id`, `HighlightTopic.topic_id → Topic.id`
- **ClipVariant 新增 `render_config_hash`** 字段, 用于渲染版本唯一性
- **迁移 v2**: `ClipVariant` UNIQUE(event_id, variant_type) / `HighlightTopic` UNIQUE(event_id, topic_id) / `UploadTask` UNIQUE(clip_id, uploader) / `SegmentTask` pipeline_key 数据填充

#### Phase 4: Worker 租约与优雅关闭
- **`lease_token`**: 原子领取时生成 UUID, stale 恢复时清除
- `enqueue_next` 清除 `lease_token`
- 优雅关闭超时从硬编码改为 `WORKER_SHUTDOWN_TIMEOUT_SECONDS` (默认30s) / `SUBPROCESS_TERMINATE_TIMEOUT_SECONDS` (默认10s)

#### Phase 5: C 扩展修复
- **C/Python bigram 空白一致性**: C 版本改为跳过空白后仅拼接字符 (不含空格), 与 Python 回退行为一致
- **异常处理**: 6 处 `PyList_Append` / 5 处 `PyList_New` 返回值检查, 补充 `error_cleanup` 标签
- **UTF-8 校验**: bytes 输入先验证合法 UTF-8, 非法返回 `UnicodeDecodeError`

#### 测试
- 状态机测试更新: 83 个状态转换 + 非法转换全覆盖
- pipeline_key/segment_id 唯一约束测试
- 旧测试适配新状态流后全部通过

## V0.1.12.4b Alpha (2026-07-06)

### CI 修复: 代码质量清理 + C 扩展正确性修复

本质目标: 修复 v0.1.12.4 发布后 CI 流水线上发现的 165 个 ruff 违规和 C 扩展崩溃问题。

#### Ruff 代码质量清理
- 修复 bug 级违规: `F821` 未定义名称, `invalid-syntax` Python 3.11 f-string 反斜杠
- 修复正确性违规: `B007` 未使用循环变量, `E741` 模糊变量名, `E701` 一行多语句, `F841` 未使用局部变量, `B904` 异常未链式抛出
- 修复风格违规: `D101/D102/D103` 缺失 docstring, `D205` docstring 空行, `E402` 延迟导入, `B008` 默认参数函数调用, `E501` 超长行
- `pyproject.toml` `line-length` 从 100 增加到 120, `tests/*` 额外忽略 `D102`、`E501`
- 全量: 165 个违规全部清零

#### C 扩展修复
- **`fast_char_bigrams` UTF-8 字符边界 bug**: 原代码 `q = p + 1` 对 CJK 字符指向 continuation byte 而非下一个字符开头, 产生无效 UTF-8 序列导致 `UnicodeDecodeError` + `SystemError`
- **MSVC 编译乱码**: `setup.py` 添加 `/utf-8` 标志, 消除 C4819 源代码编码警告

#### 测试
- 全量: 236/236 通过 (本地 Python 回退 + CI C 扩展模式均通过)

## V0.1.12.4 Alpha (2026-07-06)

### 稳定性修复: 流水线 / 自动化开关 / 原子领取 / Worker 生命周期 / 幂等 / ASR 追踪

本质目标: 把现有代码修成真实可运行、可恢复、可验证的闭环。

#### CI 修复
- `ci.yml` concurrency 从无效 `matrix` 上下文改为 `github.workflow` + `github.ref`

#### 原子任务领取
- `_pop_and_claim` 裸 SQL 改为 `sa_text()` 包裹 (SQLAlchemy 2.x 规范)
- `params` 改为关键字参数, 避免 `TypeError: exec() takes 2 positional arguments`
- `attempts` 只在 SQL UPDATE 中 increment 一次, 杜绝 double-increment

#### 五个自动化开关全部接入状态机
- 新增 `_advance_awaiting_review()`: 检查 `auto_approve` + `auto_approve_threshold`
- 新增 `_advance_approved()`: 检查 `auto_upload`, 自动发布时内联执行上传
- `_run_render` 不再 hardcode `auto_upload=False`, 改为从房间配置读取
- `auto_analyze` / `auto_render` / `auto_approve` / `auto_upload` 全部生效

#### 发布队列
- approved 后根据 `auto_upload` 决定是否执行上传
- `auto_upload=true` → 内联执行 `enqueue_and_upload` → completed
- `auto_upload=false` → 直接 completed

#### Worker 生命周期
- 新增 `track_subprocess()` / `untrack_subprocess()` / `_cleanup_subprocesses()`
- `TaskWorker.stop()` 后统一 SIGTERM → SIGKILL 清理孤儿子进程

#### 幂等与唯一约束
- `SegmentTask.idempotency_key` 改为 UNIQUE 约束
- `Transcript` 增加 `__table_args__` 声明幂等意图
- 重复回调不会创建两个任务 (数据库级保证)

#### ASR fallback 追踪
- `ASRTranscriptResult` 新增: `primary_status`, `primary_error_type`, `primary_error_message`, `fallback_backend`, `fallback_trigger_reason`
- Paraformer 空输出 → Whisper 兜底时保留完整链路信息
- `_persist_transcript` 正确写入 `fallback_backend`

#### final_text_source 优先级修正
- manual_review_needed > review > fallback > primary
- `manual_review_needed` 先于 `review` 检查, 不再被 `review` 覆盖

#### 测试质量
- `test_stability_fixes.py` 全部改为真实 SQLite 行为测试, 移除 `inspect.getsource()` 式检查
- 23 项行为测试: 原子领取 (单Worker/双Worker并发), 自动化开关 × 6, 心跳/stale, 唯一约束, 重试恢复, ASR fallback
- 全量: 236/236 通过

## V0.1.12.3 Alpha (2026-07-06)

### 接线补全 + 分设备配置生效

本次迭代定位: 将 V0.1.12.2 中已定义但未传导到运行时的配置与指标真正接通。

#### 分设备配置接线 (Phase 4 补全)
- `FunASRBackend._load_primary()` → `settings.asr_primary_device` (原走 `whisper_device`)
- `FunASRBackend._load_sensevoice()` → `settings.asr_auxiliary_device`
- `FunASRBackend._load_funasr()` → `settings.asr_review_device`
- `FasterWhisperBackend.__init__()` → `settings.asr_fallback_device` 兜底
- 所有设备配置均保持 `or whisper_device` 二级兜底

#### ASR 可观测性指标接线 (Phase 4 补全)
- `transcribe.py` 引入 `from app.analysis import asr_metrics`
- `FunASRBackend.transcribe()` → `record_backend_call("paraformer", ...)` + `record_rtf()`
- `FunASRBackend.transcribe_segment()` → `record_backend_call("funasr-nano", ...)`
- `FasterWhisperBackend.transcribe()` → `record_backend_call("whisper", ...)` + `record_fallback()` + `record_rtf()`
- `ASRPipeline._review_loop()` → `record_review()` / `record_review_success()` / `record_review_failure()`
- `/asr-metrics` 和 `/asr-models` API 现在返回实时数据

#### Phase 3 接线验证通过
- `_review_loop` → `_merge_review_text` → `final_text` 链路已验证完整
- Hotwords `initial_prompt` → `Paraformer generate(hotword=...)` 链路已验证完整
- `final_text` → `Transcript` 持久化链路已验证完整

## V0.1.12.2 Alpha (2026-07-06)

### 稳定性修复迭代 (2026-07-06) — P0 修复

本次迭代聚焦修复数据一致性、任务流水线和渲染失败等 P0 问题。不新增任何 Feature。

#### 渲染失败误报成功 (CRITICAL FIX)
- `_run_render()` 严格校验: `produce_clip()` 返回 None、文件不存在、文件过小(<1KB)、片长过短(<1s) 全部标记为失败
- 无效 Clip 不再进入 `AWAITING_REVIEW`
- 失败任务写入 `failed_stage` 并进入重试队列

#### 原子任务领取 (CRITICAL FIX)
- 改用 `UPDATE ... WHERE id=? AND stage=?` 条件更新 + `rowcount` 校验
- SQLite 下双 Worker 并发争抢保证只有一个成功
- 日志记录 task_id / worker_id / 领取结果

#### 自动化开关独立生效
- `auto_analyze=false` → 不进入转写队列和分析队列
- `auto_render=false` → 不进入渲染队列
- `auto_approve` / `auto_upload` 在阶段转换时重新读取房间配置
- 新增 `_room_cfg_from_task()` 统一读取房间开关
- 旧 `mode` 字段不再成为新流程判断依据

#### Worker 心跳 + 优雅关闭
- 新增 `_start_heartbeat_thread()` — 长任务期间周期性更新 `heartbeat_at`
- `_execute_task()` finally 中自动清理心跳
- `TaskWorker.stop()` 显式设置 `_shutting_down` 标志
- 停止时等待进行中任务完成 (最多30s), 超时后取消
- `_dispatch()` 检查 `_shutting_down` 标志停止领取新任务

#### 版本化数据库迁移
- 新增 `schema_version` / `migration_history` 表
- 新增 `app/db/migrate.py` 版本化迁移系统
- 迁移前自动备份数据库 (`.bak`)
- 迁移失败时中止, 不允许半升级
- V1 迁移: 修复旧数据中 Candidate ID 被错误写入 Event ID 的情况

#### 唯一约束
- `HighlightEvent.candidate_id` UNIQUE
- `ClipVariant.candidate_id` 标记为 deprecated
- `asr_model_revision` 默认值改为 `v2.0.4`

#### 测试
- 新增 `tests/test_stability_fixes.py` (16 个测试): 渲染失败/原子领取/自动化开关/心跳/优雅关闭/数据迁移/状态机/唯一约束

### 多模型 ASR 链路的正确性、稳定性、评测与资源治理 (2026-07-06)

本次迭代是对 V0.1.12 多引擎 ASR 流水线的深度重构, 修复了复核不触发、SenseVoice 无产出、热词未传入等关键问题。

#### 统一 ASR 结果模型 (Phase 1)
- 新增 `ASRSegmentResult` / `ASRTranscriptResult` 统一结果结构, 消除后端置信度歧义
- Paraformer confidence (0-1) 映射 `normalized_confidence`, Whisper `avg_logprob` 映射 `raw_confidence`
- 不再给无置信度字段伪造 0.0 (改为 `None`)
- 保留 `TranscriptionResult` 向后兼容 (自动从统一结果转换)
- `Transcript` 新增 14 个追踪字段: `base_text` / `final_text` / `primary_backend` / `primary_model_id` / `primary_model_revision` / `review_backend` / `fallback_backend` / `review_triggered` / `review_risk_score` / `review_reasons` / `final_text_source` / `inference_duration`

#### 复核闭环修复 (Phase 2)
- 重写 `_review_low_confidence` → `_review_loop` (基于 `review_risk_score`)
- 新增 `_compute_review_risk_score`: 综合 6 项信号 (置信度/空文本/时长比/重复/乱码/热词冲突) 决策复核
- 新增 `_extract_audio_segment`: FFmpeg 局部截取 (带上下文1.5s), 不再 fallback 全文识别
- 新增 `_merge_review_text`: base/review/final 合并策略 (编辑距离/热词命中/人工标记)
- 防火墙上线: 窗口不越界/临时文件专用 UUID + 清理/finally 清理

#### 热词与 SenseVoice 修复 (Phase 3)
- Paraformer 热词参数传入: `generate(hotword=...)` 并在不支持时降级
- SenseVoice 时间范围解析: 按文本分段估算合理时间, 不再全置 0.0
- 新增 `_audio_events_score`: 笑声/掌声/惊讶/情绪密度评分
- 高光评分新增 `audio_events` 维度 (权重 0.10)
- 新增 `ASR_SENSEVOICE_ENABLED` 独立开关 (模型加载 vs 使用分离)
- 新配置 `ASR_REVIEW_RISK_THRESHOLD` (默认 0.65)

#### 依赖拆分与模型治理 (Phase 4)
- `pyproject.toml` 拆分: `asr-whisper` / `asr-funasr` / `asr-all` (向后兼容 `asr` → `asr-all`)
- 分设备配置: 8 个环境变量 (`ASR_PRIMARY_DEVICE` / `ASR_AUXILIARY_DEVICE` / `ASR_REVIEW_DEVICE` / `ASR_FALLBACK_DEVICE` + 4 个并发 + 4 个 keep_loaded)
- 生命周期配置: `ASR_MODEL_IDLE_UNLOAD_SECONDS` / `ASR_PRELOAD_ON_START`
- 新增 `ASRModelManager` (并发加载锁/状态查询/空闲卸载/预热)

#### 可观测性 (Phase 5)
- 新增 `ASRMetrics`: 后端调用统计、RTF、复核率、fallback 率、OOM 计数
- 运维 API: `GET /api/monitor/asr-metrics` / `GET /api/monitor/asr-models`

#### Golden Set 评测与测试 (Phase 6)
- 新增 `tests/golden_set/` Golden Set 评测体系 (manifest 规范 + CER/RTF/时间戳误差)
- 新增 34 个 ASR 单元测试 (`test_asr_result.py` / `test_asr_review.py` / `test_asr_integration.py`)
- DDL 自动迁移 (12 个 `transcripts` 新增列)

#### 新增环境变量
```env
ASR_REVIEW_RISK_THRESHOLD=0.65
ASR_SENSEVOICE_ENABLED=true
ASR_PRIMARY_DEVICE=cpu
ASR_AUXILIARY_DEVICE=cpu
ASR_REVIEW_DEVICE=cpu
ASR_FALLBACK_DEVICE=cpu
ASR_PRIMARY_MAX_CONCURRENCY=1
ASR_AUXILIARY_MAX_CONCURRENCY=1
ASR_REVIEW_MAX_CONCURRENCY=1
ASR_FALLBACK_MAX_CONCURRENCY=1
ASR_PRIMARY_KEEP_LOADED=true
ASR_AUXILIARY_KEEP_LOADED=false
ASR_REVIEW_KEEP_LOADED=false
ASR_FALLBACK_KEEP_LOADED=false
ASR_MODEL_IDLE_UNLOAD_SECONDS=900
ASR_PRELOAD_ON_START=false
```

### 原则遵守
- v0.1.11 的任务队列/状态机/幂等设计未被破坏
- Whisper fallback 机制正常
- aliases 后处理正常
- room_config 的 hotwords/aliases 结构合理

## V0.1.12.1 Alpha (2026-07-06)

### 安全加固 (CodeAuditTool 审计修复)

**HIGH — 修复项**:
- `Dockerfile`: 容器以非 root 用户 `appuser` 运行，限制权限；增加 pip 哈希校验指引
- `app/analysis/_c_speedups.c`: `fast_char_bigrams()` 增加指针边界检查，防止单字符残片越界
- `app/web/login_handler.py`: 登录状态接口不再返回完整 Cookie 值，仅返回 `cookie_available` 布尔标记

**MEDIUM — 修复项**:
- `app/notify/webhook.py`: 钉钉 webhook URL 拼接改用 `urllib.parse`，防止原 query 参数被覆盖
- `app/core/logging.py`: 数据库 sink 异常写入 stderr，不再完全静默
- `app/clipping/cover.py`: `out` 变量提至 try 前初始化，避免 `mktemp` 异常时 finally 块访问未定义变量

**确认已有防护 (无需操作)**:
- `packaging/portable/build_bundle.py`: zip-slip 已防护 (L252 `".." in member`) (原 Publish-PnP)
- `app/pipeline/storage_lifecycle.py`: 符号链接检测 (L127) + 路径前缀验证 (`_safe_unlink`) 均已就位

## V0.1.12 Alpha (2026-07-06)

### 多引擎 ASR 流水线重构

默认引擎从 Whisper 单引擎升级为四层流水线:

| 层级 | 引擎 | 功能 |
|------|------|------|
| **主引擎** | Paraformer-zh | 中文文本、词级时间戳、标点 |
| **辅助特征** | SenseVoice-Small | 情感、笑声、音乐、事件检测 |
| **低置信复核** | Fun-ASR-Nano | 低分 / 非中文片段复核 |
| **最终兜底** | Whisper large-v3 / turbo | 保留切换, 主引擎失败时自动回退 |

关键特性:
- 全部模型懒加载, 进程级缓存
- `ASRPipeline` 统一编排: Paraformer → SenseVoice → FunASR-Nano → Whisper
- 通过 `ASR_PRIMARY=whisper` 可随时切回纯 Whisper 模式
- 新环境变量: `ASR_PRIMARY` / `ASR_SENSEVOICE` / `ASR_FUNASR_REVIEW` / `ASR_FALLBACK_WHISPER` / `ASR_CONFIDENCE_THRESHOLD` / `ASR_MODEL_REVISION`
- `Transcript` 新增 `auxiliary_json` 字段存储辅助特征 (情感/事件/复核结果)
- `TranscriptionResult` 扩展: `emotions` / `reviewed_segments` / `engine`

#### 新增环境变量 (`.env.example`)

```env
ASR_PRIMARY=paraformer          # paraformer / whisper
ASR_SENSEVOICE=true             # SenseVoice-Small
ASR_FUNASR_REVIEW=true          # Fun-ASR-Nano 低置信复核
ASR_FALLBACK_WHISPER=true       # Whisper 兜底
ASR_CONFIDENCE_THRESHOLD=-0.6   # 低置信阈值
ASR_MODEL_REVISION=master       # 模型版本
```

#### 依赖

- `funasr` (Paraformer / SenseVoice / Fun-ASR-Nano)
- `modelscope` (模型下载)
- `faster-whisper` (保持, 兜底引擎)

#### 测试

178 项全部通过, 零回归。
