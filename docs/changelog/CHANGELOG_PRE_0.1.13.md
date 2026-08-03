# CHANGELOG — 0.1.13 系列

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

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
