# 开发与维护

项目入口见[README](../README.md)，业务链路见[使用指南](usage.md)和[热点检测器](hotspot-detector.md)。具体 Portable 构建步骤见[构建说明](../packaging/portable/README.md)。

## 开发环境

使用 Python 3.11/3.12，准备 FFmpeg 和 Node.js。在隔离虚拟环境中安装开发依赖：

```powershell
pip install -e ".[dev,web,asr-all,llm]"
```

源码依赖要求 `sqlmodel>=0.0.22,<0.0.45`，Portable 完整锁继续使用 `0.0.39`。当前 Schema 与 API 使用既有无时区 UTC 时间约定；[SQLModel 0.0.45 起改变默认日期时间行为](https://sqlmodel.tiangolo.com/advanced/datetime/#upgrade-existing-applications)，直接升级会影响入库、查询、时间比较与响应格式。依赖上限用于保持现有数据契约，不触发数据库迁移。

Windows 的 `FFMPEG_PATH`、`FFPROBE_PATH` 应指向真实二进制文件，避免指向 Chocolatey 的 `bin` 包装程序。包装进程可能在停止时留下仍持有管道的 FFmpeg 子进程；CI 安装后从 Chocolatey 包的 `tools` 目录解析真实路径并设置这两个变量。Portable Full 已使用随包二进制的绝对路径。

Python 镜像配置见[使用指南](usage.md#python-依赖源)。普通源码运行缺少原生扩展时可按函数回退到 Python 参考实现；完整验证和 Portable 发行需要对应 ABI 的 C、Cython、Rust 三个扩展。Windows C/Cython 构建需要可用的 MSVC 工具链，Rust 需要 Rust 工具链。

```powershell
python setup.py build_ext --inplace
python tools/native/build_rust.py
```

模块职责、函数清单和诊断命令见[原生加速模块](native-acceleration.md)。开发与验收使用独立的数据库、存储和配置，不复用正在运行实例的 `.env`、Cookie、数据库或媒体。

## 验证

先执行改动对应的目标测试，再运行完整测试和质量检查。项目的 `pytest` 默认收集 `tests/` 与 `packaging/portable/tests/`：

```powershell
python -m pytest
python scripts/run_ruff.py check
python scripts/run_ruff.py format
python scripts/check_version_consistency.py
python scripts/check_changelog_archive.py
node scripts/check_frontend_interactions.mjs
node scripts/check_configuration_interactions.mjs
```

完整门禁需要可用的原生扩展、真实 Payload 等前置产物，缺少前置条件时应先完成构建；不将跳过项目计为通过。以下入口按仓库配置执行依赖审计、测试和相关构建检查：

```powershell
# 提交前 CI 门禁
python scripts/ci_gate.py
# 发布前门禁：拒绝测试跳过、无效审计结果及不完整或不可复现的产物
python scripts/release_gate.py
```

真实高光插件联调需要单独提供插件仓库，命令见[使用指南](usage.md#可插拔高光评分)，不属于宿主默认测试集。

## 版本与源码冻结

- 应用版本以 [`app/__init__.py`](../app/__init__.py) 为事实来源。升级时同步包元数据、原生模块、Portable 配置、文档与产物命名，再执行版本一致性检查。模型 revision 和 schema 根据真实契约变化维护，不随补丁版本机械递增。
- **Source Commit** 标识内嵌业务源码快照，**Builder Commit** 标识本次运行构建工具的提交。先提交需要进入 Payload 的业务源码与版本，再更新 Portable 的源码冻结配置；构建工具和文档可以在后续提交维护。
- Payload 构建检查其包含范围内的已跟踪差异和新增未跟踪文件，拒绝工作区业务源码与冻结提交不一致。范围由[源码快照模块](../packaging/portable/src/blc_portable/payload/source_snapshot.py)定义，不通过忽略检查或手工复制工作区文件绕过。
- 快照通过 Git 提取，构建只校验其自身声明的版本，不在构建期改写旧源码版本。默认重新提取、构建两轮并比较 ZIP SHA-256；原生模块在构建 checkout 中编译，随后复制匹配 ABI 的产物到打包目录。

## 发行契约

- Payload 保存文件清单与 SHA-256，Runtime 在安全解压和验证后通过 staging、锁和原子切换激活；用户配置、数据库、模型和媒体不随业务源码覆盖。具体目录布局见[Portable 构建说明](../packaging/portable/README.md)。
- 数据库、Runtime、Payload 和模型清单遵循当前明确支持的格式；不能从历史重构记录推断迁移能力。模型按模型目录与内容身份独立复用，具体校验由当前安装器执行。
- 真实 Engine Pack 与 fixture 必须明确区分。Fixture 的摘要来自实际构建文件；它只用于验证，不能作为正式模型包分发。主模型、子模型和随附组件使用统一模型目录，保留所需许可证和来源证据。
- 发布清单记录最终文件名、版本、大小、SHA-256 和 CRC32；跨制品检查源码、构建器、Payload 与版本身份。重新构建后的摘要必须重新计算，并检查源码路径、敏感配置、缓存和临时文件未进入产物。

## 文档约定

- README 提供项目简介、核心功能、快速开始、重要限制和文档入口；详细用法进入对应指南，避免每个版本继续追加历史小节。
- 用户可见变更记入当前系列的 `CHANGELOG.md`；旧系列使用既有 `docs/changelog/` 归档，不重复维护多份版本历史。
- 长期有效的配置说明、架构决策和发行约束留在正式文档，随实现同步维护。一次性任务清单、排查过程和运行日志可留在 PR、Issue 或本地 `.local/` 中，不作为当前使用指南。
- `.local/` 从 Git 跟踪、源码分发包和 Docker 构建上下文中排除。整理历史记录时先提炼仍有效的内容，核对引用，再保留本地原件；不要通过重写 Git 历史删除已经提交的记录。
