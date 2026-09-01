# CHANGELOG — 0.1.14 系列

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

## V0.1.14.12 Alpha (2026-07-21)

### 修复

- **release**: Full Bundle 的 `app.cli` 冒烟测试改用已完成离线依赖安装的 `$venvPython`，避免 runner 裸 Python 缺少 `typer` 导致发布误失败
- **release**: 增加工作流契约测试，禁止 `app.cli` 冒烟检查退回裸 `python`

## V0.1.14.11 Alpha (2026-07-17)

### 修复

- **database**: Schema 指纹去除 callable 内存地址并稳定排序约束，修复数据库重建后重启即被误判为不兼容
- **pipeline**: 修复转写提交写入不存在的 `SegmentTask.transcript_id` 并向 `enqueue_next()` 传入无效参数
- **cli**: `record --pipeline/--produce` 同步房间调度开关并传递房间主键，拒绝单独使用 `--produce`
- **asr**: 修复兼容入口从错误模块导入 `TranscriberBackend` 导致编排器无法加载
- **ci**: 覆盖率运行器同时校验 pytest 退出码，移除破坏默认配置测试的空 `WHISPER_MODEL`
- **release**: 正式构建禁止 fixture 绕过，修复标签门禁、Full ZIP/CLI smoke、产物聚合与校验和生成
- **version**: `__version_label__` 改为从版本真源动态生成，Docker 文档同步至当前版本
- **pipeline**: 修复 `acquire_resources()` 返回 bool 但被当作 dict 传递给 `release_resources(**cost)` 的 TypeError
- **models**: 删除空 `ENGINES_TO_DOWNLOAD=[]`，替换为 `_load_launcher_engines()` 加载统一 Catalog
- **models**: 子模型目录使用 `target_subdir` 而非完整 repository ID，防止目录名错误
- **asr**: 修复 `iic/Fun-ASR-Nano` → `FunAudioLLM/Fun-ASR-Nano-2512` 正确仓库 ID（backends.py 和 pipeline.py）
- **engine-pack**: Schema 升级至 v4，统一 Builder/Installer/Verifier 版本号
- **engine-pack**: 新增 `artifact_class` 字段区分 `production`/`fixture`
- **runtime**: 嵌入式 Payload identity 与 installed current.json 比对新旧 EXE
- **portable**: 删除 `requirements-bundle.txt` 及 wheels/mirror 死代码，只用 ABI 锁文件 + `--require-hashes`
- **full**: 删除 `continue-on-error`，wheels 从 lock 文件下载，零 wheel 立即失败
- **web**: 实现真实 CSRF 防护（Origin/Referer 校验、Basic Auth 后仍检查同源）
- **web**: Basic Auth 用户名和密码都必须校验
- **native**: 删除 `/arch:AVX2` 强制编译标志，使用 generic x86-64 基线
- **c**: 修复 `ac_build_failure()` 返回 void 但调用方未检测 `PyErr_Occurred()` 的 bug
- **release.yml**: 新增 production metadata 完整校验（嵌套哈希、commit、engine_ids、size）

## V0.1.14.8 Alpha (2026-07-15)

### 修复

- **builder**: 修复 `engine_pack_info.json` 缺失 `sha256`/`size_bytes`/`source_commit` 字段，导致 Lite EXE 构建校验失败
- **full.py**: 英文化所有运行时 print 语句避免 Windows CI cp1252 `UnicodeEncodeError`
- **release.yml**: 移除冗余 `certutil` checksums 步骤，拆分 Full 离线组件准备为独立步骤
- **README**: 新增 Engine Pack 本地生成说明，明确 GitHub Release 不包含 ASR 模型引擎包

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

历史版本归档见 [docs/changelog/CHANGELOG_INDEX.md](docs/changelog/CHANGELOG_INDEX.md)。
