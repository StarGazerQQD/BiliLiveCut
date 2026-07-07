# Changelog — 0.1.10 系列 (已归档)

> 此文件已从主 CHANGELOG.md 归档。原始版本详见 Git 历史。

## V0.1.10.1 Alpha (2026-07-05)

### 全量审计修复

V0.1.10 引入 Rust+rayon 并行聚类矩阵后,全量代码审计发现并修复 2 项 bug:

#### 修复

| # | 严重度 | 文件:行 | 问题 | 修复 |
|---|--------|---------|------|------|
| C1 | **Critical** | `topic_cluster.py:255` | `n` 变量未定义 — V0.1.10 替换为 `cluster_similarity_matrix(items)` 时删除了原 `n = len(items)` 行,导致后续 `range(n)` 报 `NameError`,主题聚类功能完全不可用 | 在 `matrix = cluster_similarity_matrix(items)` 之前恢复 `n = len(items)` |
| H1 | **High** | `monitor_router.py:45` | `_last_disk_alert = _now` 中 `_now` 未定义 — V0.1.9.1 审计修复了第 38 行 (`time.time()`) 但遗漏了第 45 行赋值语句 | 改为 `_last_disk_alert = time.time()` |

### 安全加固

| # | 文件 | 修改 |
|---|------|------|
| S1 | `storage_lifecycle.py` | 新增 `_safe_unlink()` helper — `unlink` 前验证 `resolve()` 路径在 `clips_dir()` 前缀下,拒删非托管路径,防止路径遍历攻击 |
| S2 | `storage_lifecycle.py:104` | `shutil.rmtree` 前增加 `is_symlink()` 检查,跳过符号链接目录,防止通过符号链接逃逸到外部目录 |
| S3 | `storage_lifecycle.py:141-149` | `cleanup_rejected_candidates` 的 `file_path` 和 `cover_path` 删除改用 `_safe_unlink` |
| S4 | `build_bundle.py:252` | `_extract_ffmpeg_from_zip` 增加 `if ".." in member: continue` 过滤,防止 ZipSlip 路径遍历 |

#### 测试

- 全量 161 项通过,零回归

---

## V0.1.10 Alpha (2026-07-05)

### 第二轮 C/Rust/Cython 加速 — 聚类矩阵 + 弹幕基线 + SRT 组装

基于 `V0.1.9` 全量性能审计,对剩余 3 个 CPU 瓶颈实施第二轮加速:

#### 加速热点

| 模块 | 热点 | 原实现 | 新实现 | 预期提速 |
|------|------|--------|--------|----------|
| `topic_cluster.py` | O(N²) 聚类矩阵构建 | `event_similarity` 重复计算 + Python 浮点矩阵 | 预提取 bigram/kw 向量 + 单遍 `_pairwise_sim` | **5–15×** |
| `highlight.py` | `_danmaku_baseline` 分桶+中位数 | `datetime` 对象热循环 + `timedelta` 算术 | `danmaku_baseline_rate` — 纯 float 分桶+排序 | **10–30×** |
| `clipper.py` | `_group_srt` 词条→SRT 组装 | `divmod`+`f-string` 逐行格式化 | `group_srt_blocks` — 单遍聚合+手动 fmt | **3–8×** |

#### 新增文件

- `app/analysis/_speedups_round2.pyx` — Cython 源码 (A 聚类矩阵 + B 弹幕基线 + C SRT 组装)
- `app/analysis/_speedups_round2_py.py` — 纯 Python 后备 (Cython 不可用时自动使用)
- `app/analysis/_rust_src/` — **Rust 加速源码** (PyO3 + rayon 并行 N² 聚类矩阵,自动检测编译)
- `build_rust.py` — Rust 编译脚本 (`python build_rust.py` → 自动检测 cargo + 编译 + 复制 .pyd)

#### 修改文件

- `app/analysis/speedups.py` — 分派层重构为**三级加速链**: Rust (并行) → Cython → Python,新增 `get_cluster_backend()` 诊断接口
- `app/analysis/topic_cluster.py` — 聚类矩阵构建替换为 `cluster_similarity_matrix(items)`
- `app/analysis/highlight.py` — 弹幕基线计算替换为 `danmaku_baseline_rate`
- `app/clipping/clipper.py` — SRT 组装替换为 `group_srt_blocks`
- `.gitignore` — 新增 Rust `target/` 编译缓存忽略

#### 审计发现与修复

- **Rust IDF bug (`lib.rs:70`)**: `idf_weight` 使用 `all_keys.contains(k)` (union) 替代交叉检查 `other.contains_key(k)`,导致 IDF 惩罚恒为 1.0。已修复为传递 `other` 参数双向检查。

#### 测试

- 全量 161 项通过,零回归,零新增 bug

#### 设计原则

- **零用户配置**: 有 Cython 编译环境时自动编译,无时自动回退 Python
- **API 兼容**: 新函数签名与原函数等价,行为一致
- **`_group_srt` 保留**: 旧的 `_group_srt` 函数保留在 `clipper.py` 中以兼容测试导入

---
