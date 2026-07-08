# BiliLiveCut v0.1.14.6-alpha 发行结构重构 — 最终报告

**生成日期**: 2026-07-08
**起始基线**: 731a31cd04ae1df27dd6b6c5ffc535123932b825 (short: 731a31c)
**最终 Builder Commit**: b554cca60393c7ce7617007ddd8e074f0ccfa03b

---

## 提交历史 (6 个中文提交)

1. f5de5bf 升级版本：更新至 v0.1.14.6-alpha
2. 6d47e2a 重构 Docker：迁移发行文件至 packaging/docker/
3. 9bbf058 重构原生构建：迁移 Rust 构建脚本至 tools/native/
4. 400ceb5 重构 Portable：整理构建与启动代码至 src/blc_portable/
5. 0220d07 新增模型包：实现四引擎 Engine Pack 增强功能
6. b554cca 完善发行与测试：修复 Portable 导入、Ruff 合规

---

## 版本
- 发布版本: 0.1.14.6-alpha ✅
- pyproject.toml 保持根目录 ✅
- 无第二份 pyproject.toml ✅

## Docker
- Dockerfile: packaging/docker/Dockerfile ✅
- Compose: packaging/docker/compose.yaml ✅
- .dockerignore: 新建在根目录 ✅
- 便捷脚本: scripts/docker-up|down.{sh,bat} ✅

## Rust
- build_rust.py: tools/native/build_rust.py ✅
- 所有引用路径已更新 ✅

## Portable
- 可导入代码: src/blc_portable/ (5 个子包) ✅
- 根构建脚本改为薄入口 ✅
- 无 packaging/__init__.py ✅
- PyInstaller spec 更新至 specs/ ✅
- Config: env.example, pip.ini, model_sources.json ✅
- Engine Pack splitter (1.8GiB 分卷) ✅

## 四引擎 ASR
- Paraformer-zh: v2.0.4 (ModelScope) / Apache-2.0 ✅
- SenseVoice-Small: default (ModelScope) / Apache-2.0 ✅
- Fun-ASR-Nano: default (ModelScope) / Apache-2.0 ✅
- Whisper: default (HuggingFace) / MIT ✅

## 测试
- 全量 pytest (tests/): ~320 tests PASS ✅
- Ruff check: All checks passed ✅
- Ruff format: 通过 ✅

## NOT EXECUTED (环境限制)
- Docker Build/Compose 测试 (无 Docker)
- Rust 构建测试 (无 Rust 工具链)
- Engine Pack 完整构建 (需下载数 GB 模型)
