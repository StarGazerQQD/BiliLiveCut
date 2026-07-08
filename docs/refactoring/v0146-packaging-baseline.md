# BiliLiveCut v0.1.14.6 发行结构重构 — 基线审计报告

**生成日期**: 2026-07-08
**起始 Commit**: `731a31c`
**当前 HEAD**: `731a31cd04ae1df27dd6b6c5ffc535123932b825`
**工作区状态**: 干净 (无未提交修改)

---

## 1. 当前版本号来源

| 位置 | 当前值 |
|------|--------|
| `app/__init__.py` | `0.1.14.5-alpha` |
| `pyproject.toml` | `0.1.14.5-alpha` |
| `setup.py` | `0.1.14.5-alpha` |
| `setup_c.py` | `0.1.14.5` |
| `README.md` | `V0.1.14.5 Alpha` |
| `CHANGELOG.md` | `V0.1.14.5 Alpha` |
| `.github/workflows/ci.yml` | `V0.1.14.5` |
| `packaging/portable/launcher.py` | `V0.1.14.5 Alpha` / `0.1.14.5-alpha` |
| `packaging/portable/build_engine_pack.py` | `0.1.14.5-alpha` |
| `packaging/portable/build_exe.py` | `0.1.14.5-alpha` |
| `packaging/portable/build_full_bundle.py` | `0.1.14.5-alpha` |
| `packaging/portable/build_payload.py` | `0.1.14.5-alpha` |
| `packaging/portable/engine_pack_manifest.py` | `0.1.14.5-alpha` |
| `packaging/portable/payload_manifest.py` | `0.1.14.5-alpha` |
| `packaging/portable/runtime_layout.py` | `0.1.14.5-alpha` |
| `packaging/portable/portable_launcher.spec` | `v0.1.14.5-alpha-x64` |
| `packaging/portable/tests/test_engine_pack.py` | `0.1.14.5-alpha` |
| `packaging/portable/tests/test_portable.py` | `0.1.14.5-alpha` |
| `packaging/portable/resources/engine_pack_info.json` | `0.1.14.5-alpha` |

## 2. pyproject.toml

- **位置**: 根目录 `./pyproject.toml`
- **职责**: 项目元数据、依赖声明、构建系统、Ruff/Pytest 配置
- **无第二份** pyproject.toml

## 3. Docker 现状

- `Dockerfile`: 根目录 `./Dockerfile`
- `docker-compose.yml`: 根目录 `./docker-compose.yml`
- `.dockerignore`: **不存在** (需新建或确认不需要)
- Build Context: 仓库根目录
- Dockerfile COPY: `pyproject.toml README.md ./, app ./app, config ./config`
- Compose volumes: `./storage:/data`
- Compose env_file: `.env`

## 4. Rust 构建现状

- `build_rust.py`: 根目录 `./build_rust.py`
- Rust 源码: `app/accelerators/rust/` (Cargo.toml, src/lib.rs)
- 构建方式: `cargo build --release` 在 `app/accelerators/rust/` 目录
- 产物目标: `app/analysis/_rust_cluster.pyd` (Windows) / `.so` (Linux)

### 引用 `build_rust.py` 的位置:
- `README.md`:118
- `CHANGELOG.md`:151
- `app/accelerators/rust/src/lib.rs`:13
- `app/analysis/_rust_src/src/lib.rs`:13
- `docs/changelog/CHANGELOG_PRE_0.1.10.md`
- `packaging/portable/.gitignore`:21 (排除项)
- `packaging/portable/README.md`:136
- `packaging/portable/build_payload.py`:47 (Payload 白名单)
- `packaging/portable/source_snapshot.py`:29 (Payload 白名单)

## 5. Portable 现状

### 文件树 (17 个 .py 文件, 扁平结构):

```
packaging/portable/
├── __init__.py
├── launcher.py
├── payload_manifest.py
├── source_snapshot.py
├── build_payload.py
├── build_exe.py
├── build_full_bundle.py
├── build_engine_pack.py
├── build_bundle.py
├── engine_pack.py
├── engine_pack_manifest.py
├── model_installer.py
├── download_engines.py
├── runtime_layout.py
├── portable_launcher.spec
├── pip.ini
├── .env.example
├── .gitignore
├── README.md
├── requirements-bundle.txt
├── resources/
│   ├── engine_pack_info.json
│   └── ...
└── tests/
    ├── __init__.py
    ├── test_portable.py
    └── test_engine_pack.py
```

- **无 `src/blc_portable/` 子目录** — 所有代码扁平放置
- **无 `pytest --collect-only` 统计** — 待添加

## 6. 四引擎 ASR 模型配置

基于 `app/analysis/transcription/backends.py` 和 `app/core/config.py`:

| 引擎 | engine_id | model_id | 代码模型 ID | Revision | 来源 |
|------|-----------|----------|------------|----------|------|
| Paraformer-zh | paraformer | paraformer-zh | `iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch` | v2.0.4 | ModelScope |
| SenseVoice-Small | sensevoice | iic/SenseVoiceSmall | `iic/SenseVoiceSmall` | v2.0.4 | ModelScope |
| Fun-ASR-Nano | funasr_nano | iic/Fun-ASR-Nano | `iic/Fun-ASR-Nano` | v2.0.4 | ModelScope |
| Whisper | whisper | large-v3-turbo | `mobiuslabsgmbh/faster-whisper-large-v3-turbo` | N/A | HuggingFace |

Paraformer 子模型: `fsmn-vad`, `ct-punc`, `cam++` (均 ModelScope, v2.0.4)

Revision 来源: `settings.asr_model_revision = "v2.0.4"`

## 7. Source Commit 引用

当前活动引用 `74c21b4` 的位置 (需更新为 `731a31c`):
- `packaging/portable/launcher.py`:30
- `packaging/portable/source_snapshot.py`:3,191,192,210,211,221,223,240,241
- `packaging/portable/payload_manifest.py`:15-16
- `packaging/portable/build_payload.py`:5,142,145
- `packaging/portable/build_engine_pack.py`:48,363
- `packaging/portable/engine_pack_manifest.py`:4,31,231
- `packaging/portable/build_full_bundle.py`:97,111
- `packaging/portable/runtime_layout.py`:17
- `packaging/portable/portable_launcher.spec`:5
- `packaging/portable/tests/test_portable.py`:4,69,72,82,83,93,107,142,256
- `packaging/portable/tests/test_engine_pack.py`:71-72,162,179,195-196,213-214
- `packaging/portable/README.md`:多处
- `README.md`:22
- `CHANGELOG.md`:14,27
- `.github/workflows/ci.yml`:220
- `docs/refactoring/portable-build-final-report.md`:多处

## 8. GitHub Release 工作流

- `.github/workflows/release.yml`: 构建 sdist + wheel + 生成 SHA256
- `.github/workflows/ci.yml`: lint + audit + test (matrix) + portable-test + coverage

## 9. 测试基线

测试收集完成，已保存到 `tests-before-v0146.txt`。
测试分布:
- `tests/fault_injection/`: 101 tests
- `tests/integration/`: 101 tests
- `tests/unit/`: 116 tests
- `tests/golden_set/`: 2 items
- Total: ~320 test nodes (需精确统计)

## 10. 受影响的文档和脚本

- `README.md`
- `CHANGELOG.md`
- `packaging/portable/README.md`
- `packaging/portable/.gitignore`
- `.github/workflows/ci.yml`
- `.github/workflows/release.yml`
- `setup.py`, `setup_c.py`
- `scripts/check_version_consistency.py`
- 所有历史 CHANGELOG 文件

## 11. 关键发现与风险

1. **无 `.dockerignore`** — Docker 构建时可能包含构建产物和临时文件
2. **Portable 代码扁平** — 需重构为 `src/blc_portable/` 模块化结构
3. **多处硬编码** 74c21b4 和 0.1.14.5-alpha — 需批量更新
4. **CI 引用旧 commit** — `.github/workflows/ci.yml:220` 引用 `74c21b4`
5. **模型 revision** — `asr_model_revision = "v2.0.4"` 作为默认值
6. **无 `pytest --collect-only` 完整输出** — 需精确统计测试 Node ID

## 12. 审计结论

当前仓库状态干净 (`731a31c` HEAD)，所有文件均为受跟踪文件。未发现结构性问题，可以开始重构。

下一步：升级版本到 `v0.1.14.6-alpha`。
