# 开发与维护

项目入口见[README](../../README.md)，业务链路见[使用指南](../features/README.md)和[热点检测器](../reference/hotspot-detector.md)。具体 Portable 构建步骤见[构建说明](../../packaging/portable/README.md)。

## 开发环境

Windows CUDA 运行时额外使用 `packaging/portable/locks/requirements-torch-{cu126|cu130}-{py311|py312}-win-x64.lock`，只替换 Torch/TorchAudio。更新基础锁中的这两个版本时，必须从 PyTorch 官方索引同步 CUDA URL、SHA-256 和 ABI，核对 wheel 元数据依赖，并运行 `packaging/portable/tests/test_torch_runtime.py`；不放宽哈希检查或自动接受未登记的 CUDA 版本。源码环境应用 CUDA 锁后，以现有虚拟环境的 Python 运行，安装说明见[使用指南](../features/asr.md#cuda-安装与实际设备确认)。

使用 Python 3.11/3.12，准备 FFmpeg 和原生扩展所需的编译工具。uv 和传统 venv + pip 均受支持，共用 `pyproject.toml` 中的依赖与 extras。Web 控制台使用独立 React 前端，开发与构建需要 Node.js 24。运行已构建页面不需要 Node.js，目录和验证方式见 [Web 页面说明](frontend.md)。

### uv

先按 [uv 官方说明](https://docs.astral.sh/uv/getting-started/installation/)安装 uv，当前 CI 验证版本为 `0.12.18`。在项目根目录执行：

```bash
uv sync --locked
uv run --locked pre-commit install
uv run --locked python -m pytest tests/unit/test_native_parity.py
uv run --locked pre-commit run --all-files --show-diff-on-failure
```

`.python-version` 默认选择 Python 3.12，不改变包的 `>=3.11` 要求；需要其他版本可用 `uv sync --locked --python 3.11`。`uv sync` 创建 `.venv` 并以 editable 方式安装项目，无需手动激活环境。[默认 dev 依赖组](https://docs.astral.sh/uv/concepts/projects/dependencies/#default-groups)复用原有 `dev`、`web` extras；setuptools 和 wheel 与现有可复现 bootstrap wheel 构建器的固定版本一致，不默认安装 FunASR/LLM 或下载转写模型。

需要完整业务依赖时，同步与运行都传入所需 extras：

```bash
uv sync --locked --extra asr-all --extra llm
uv run --locked --extra asr-all --extra llm blc start
```

`uv run` 会同步环境，省略 extras 可能移除此前安装的可选依赖；已同步后可用 `uv run --no-sync ...` 运行当前环境。纯运行环境使用 `uv sync --locked --no-dev --extra web`，对应命令为 `uv run --locked --no-dev --extra web blc ...`。

`blc` 在解析参数前将标准输出及标准错误流设为 UTF-8，帮助与错误信息中的中文不依赖 Windows 当前代码页；重定向日志也应按 UTF-8 读取。嵌入式调用可继续使用 `app.cli.app`，由宿主管理输出流。

`uv.lock` 纳入版本管理，锁定源码环境的依赖；Portable 的 Windows ABI 锁仍按原流程维护。修改依赖后执行 `uv lock` 并一起提交 `pyproject.toml` 和 `uv.lock`；有意升级单个包时用 `uv lock --upgrade-package 包名`。[`--locked` 会拒绝过期锁文件](https://docs.astral.sh/uv/concepts/projects/sync/#checking-the-lockfile)，CI 在 Linux/Windows 验证锁文件、运行与开发安装、原生扩展导入和目标测试。原有 pip 多 Python 测试矩阵继续运行。

### venv + pip

先用 `python -m venv .venv` 创建环境，Linux/macOS 执行 `source .venv/bin/activate`，PowerShell 执行 `.\.venv\Scripts\Activate.ps1`，再安装开发依赖：

```powershell
python -m pip install "setuptools>=77"
pip install -e ".[dev,web,asr-all,llm]"
python -m pre_commit install
```

`pre-commit install` 为当前 checkout 安装提交钩子；每次克隆后执行一次。首次运行需要联网准备隔离的 hook 环境，之后复用缓存。项目的 Python 检查脚本不需要在 hook 环境安装应用或下载模型；前端检查使用 PATH 中的 Node.js。

提交时以只读模式检查暂存文件的 Ruff lint/格式、JSON/YAML/TOML、冲突标记和新增文件大小（上限 1 MiB），并运行项目版本一致性、changelog 归档和快速发行契约审计。React 文件变更时执行前端类型、lint 与单元测试；后端 API 变化还需更新 OpenAPI 类型。需要修复时，对目标文件显式执行 Ruff 修复或格式化，检查 diff 后再暂存。

Ruff 与 pre-commit 的版本固定在项目配置中，外部 hook 和新增 Action 使用经核对的完整 Git 提交；更新 Ruff 时同步修改 `pyproject.toml`、hook 提交与 `additional_dependencies`。更新 pre-commit 时同步修改开发依赖、配置最低版本和 CI 安装版本。新增超过上限且确需入库的文件应明确调整 hook 配置；录像、模型及发行产物应继续放在存储或构建目录中。

源码依赖要求 `sqlmodel>=0.0.22,<0.0.45`，Portable 完整锁继续使用 `0.0.39`。当前 Schema 与 API 使用既有无时区 UTC 时间约定；[SQLModel 0.0.45 起改变默认日期时间行为](https://sqlmodel.tiangolo.com/advanced/datetime/#upgrade-existing-applications)，直接升级会影响入库、查询、时间比较与响应格式。依赖上限用于保持现有数据契约，不触发数据库迁移。

Portable 依赖锁固定 `hydra-core==1.3.7` 和 `urllib3==2.8.0`，对应 [Hydra 安全修复](https://github.com/hydra-ecosystem/hydra/releases/tag/v1.3.7)与 [urllib3 安全修复](https://github.com/urllib3/urllib3/releases/tag/2.8.0)。两者沿用现有依赖范围，纯 Python wheel 适用于两个 Windows ABI；更新时核对实际下载文件的 SHA-256，运行双 ABI 依赖解析及审计。Hydra 会拒绝危险配置目标，urllib3 更严格地隔离 HTTPS 代理和目标站点 TLS 配置；自定义第三方配置不能依赖旧的不安全行为。

Windows 的 `FFMPEG_PATH`、`FFPROBE_PATH` 应指向真实二进制文件，避免指向 Chocolatey 的 `bin` 包装程序。包装进程可能在停止时留下仍持有管道的 FFmpeg 子进程。Windows CI 共用 `scripts/download_release_ffmpeg.py` 的 Release 下载入口，沿用 BtbN/Gyan 来源、每源最多三次尝试及 ZIP 完整性校验，绕开 Chocolatey 服务超时后返回成功但未安装文件的问题。通过 `--github-env` 指定 Actions 环境文件时，脚本先执行两个二进制的版本探测并确认 `subtitles`、`drawtext` 滤镜，全部成功后才写入真实绝对路径，并把已验证目录加入后续步骤的 PATH；失败保留具体原因并返回非零状态。Portable Full 已使用随包二进制的绝对路径。

### 原生扩展构建与安装排错

重复检测扩展按语言分别编译：libsais 保持 C 源码，Cython 包装与内核使用 C++17，再链接为同一扩展。C++ 标准参数不会传入 C 编译器。macOS CI 使用 Clang 构建并强制验证三个原生扩展可加载，然后运行完整测试；Python 回退安装继续由独立矩阵验证。

Python 镜像配置见[使用指南](../guide/getting-started.md#python-依赖源)。普通源码运行缺少原生扩展时可按函数回退到 Python 参考实现；完整验证和 Portable 发行需要对应 ABI 的 C、Cython、C++、Rust 四个扩展。Windows C/Cython 构建需要可用的 MSVC 工具链，Rust 需要 Rust 工具链。

默认安装编译 C/Cython；uv 与 pip 使用相同构建后端。Ubuntu 使用系统 Python 3.12 时，`Python.h: No such file or directory` 表示缺少开发头文件，可执行 `sudo apt-get install build-essential python3.12-dev`（其他 Python 版本安装对应的开发包）。uv 也可管理 Python：`uv python install 3.12` 后使用 `uv sync --locked --managed-python`，仍需 C 编译器；若已有使用系统 Python 的 `.venv`，应先退出并将该环境移走，再创建新环境。

不需要原生加速时，可以显式跳过扩展安装：

```bash
# Linux/macOS
BLC_SKIP_C_EXTENSIONS=1 uv sync --locked
# pip 同样支持此开关
BLC_SKIP_C_EXTENSIONS=1 python -m pip install -e ".[dev,web]"
```

PowerShell 对应执行 `$env:BLC_SKIP_C_EXTENSIONS = "1"` 后再安装。uv 运行命令时保持该环境变量；恢复原生构建先取消它（PowerShell：`Remove-Item Env:BLC_SKIP_C_EXTENSIONS`），再执行 `uv sync --locked --reinstall-package bili-live-cut`。uv 构建缓存跟踪此开关及 C/Cython 源文件，切换模式后不会沿用不匹配的构建缓存；本地已生成的扩展文件不会因此删除。纯 Python 安装不代表完整原生发行验证通过。

使用 uv 编译时，在下列 Python 命令前加 `uv run --locked`：

```powershell
python setup.py build_ext --inplace
python tools/native/build_rust.py
```

原生构建参数测试通过 `distutils.ccompiler.new_compiler` 创建真实编译器，并在 `subprocess.check_call` 边界记录编译与链接命令，避免私有类的重复导入或内部进程方法变化影响 setuptools 版本间的验收；C/C++ 参数、对象链接与扩展元数据恢复均保留断言。

模块职责、函数清单和诊断命令见[原生加速模块](../reference/native-acceleration.md)。开发与验收使用独立的数据库、存储和配置，不复用正在运行实例的 `.env`、Cookie、数据库或媒体。

wheel 安装后，直播源接口文档位于 `share/bili-live-cut/docs/development/`，插件示例说明位于相邻的 `share/bili-live-cut/plugin/`；分发测试使用真实安装命令核对两者之间的相对链接与源文档内容。

## 验证

先执行改动对应的目标测试，再运行完整测试和质量检查。项目的 `pytest` 默认收集 `tests/` 与 `packaging/portable/tests/`：

CI 使用 `pip install -e ".[dev,web]"` 安装依赖。`dev` 复用 `asr-whisper` extra，包含无语音分类与独立 ASR 子进程回归需要的真实音频解码、CPU Silero VAD 及其 SDK 依赖；这些测试不需要 GPU，也不下载转写模型。验证依赖变更时应新建隔离虚拟环境，避免开发机已安装的 ASR SDK 掩盖声明遗漏；CI 保持 `ASR_NO_MODEL_DOWNLOAD=1`。

`asr-whisper` 显式要求 `av>=11,<19`。当前 faster-whisper 1.2.1 仍向 `av.open()` 传入 `metadata_errors`，而 PyAV 19 已移除此参数（见[上游兼容性问题](https://github.com/SYSTRAN/faster-whisper/issues/1492)）；无上限安装会在音频解码前抛出 `TypeError`。Portable 的两个 ABI 锁继续使用已验证的 `av==18.0.0`。将来放开上限前，应先验证真实解码、CPU VAD 及独立子进程回归。

```powershell
python -m pytest
python -m pre_commit run --all-files --show-diff-on-failure
```

提交钩子、CI 的 lint job 和发布前检查共用 `.pre-commit-config.yaml`。只验证 React 前端可执行 `python scripts/check_react_frontend.py`。完整测试、覆盖率、联网依赖审计和 Portable 构建由 CI/发布门禁执行，不放进每次提交。文档包含版本信息并参与源码发行，因此 Markdown 修改也触发 CI 的检查。

CI 的每周定时任务只运行依赖审计，避免无源码变更时重复执行整套测试与 Portable 构建。各矩阵覆盖率文件按目录保存；`CI status` 汇总所有适用 job 的结果，可作为分支保护的必需检查。是否启用分支保护由仓库管理员配置。

GitHub Actions 的 macOS 全量测试在 `main` 推送、面向 `main` 的 PR 和手动运行 CI（`workflow_dispatch`）时执行；三种事件的 `CI status` 都要求 macOS 成功，不接受跳过，从而在合并前发现平台问题。macOS 先运行进程清理与真实服务退出回归，失败时保留子进程诊断，再执行完整覆盖率测试。覆盖率测试步骤上限为 90 分钟，整个 job 上限为 120 分钟，为依赖安装和报告上传保留余量。

macOS 字幕和片头测试需要带 `subtitles`、`drawtext` 滤镜的 FFmpeg。CI 安装 Homebrew 的 [`ffmpeg-full`](https://formulae.brew.sh/formula/ffmpeg-full)，通过 `brew --prefix --installed ffmpeg-full` 解析路径，并显式设置 `FFMPEG_PATH`、`FFPROBE_PATH` 和后续步骤的 `PATH`。该配方为 keg-only，仅安装它不会替换默认路径中的普通 `ffmpeg`；普通版缺少这些滤镜时会导致真实烧录测试失败。安装步骤检查两个可执行文件和所需滤镜，缺失时在完整测试前直接报错，不跳过字幕或片头测试。

测试重试和退避时，应先隔离被测模块持有的时钟与随机数引用，再替换等待或随机函数；直接 patch 模块引用中的标准库属性会影响整个进程，可能把后台线程的等待误计为重试。数据库退避测试会主动启动另一个线程，验证它的等待不会进入重试记录。

完整门禁需要可用的原生扩展、真实 Payload 等前置产物，缺少前置条件时应先完成构建；不将跳过项目计为通过。以下入口按仓库配置执行依赖审计、测试和相关构建检查：

```powershell
# 提交前 CI 门禁
python scripts/ci_gate.py
# 发布前门禁：拒绝测试跳过、无效审计结果及不完整或不可复现的产物
python scripts/release_gate.py
```

`dev` extra 包含门禁使用的 `pytest-timeout`。本地 CI 门禁和发布门禁均拒绝测试跳过；本地 CI 门禁先构建 Windows Payload，再执行 Portable 测试，构建失败时拒绝复用旧产物。完整 Portable 检查需要 Windows 和相应原生构建工具；其他平台可运行 `python scripts/ci_gate.py --skip-portable`。所有 `--skip-*` 模式都会明确标记为部分检查通过，不代表完整 CI 门禁通过。

真实高光插件联调需要单独提供插件仓库，命令见[使用指南](../features/highlight-plugins.md)，不属于宿主默认测试集。

受监管进程树的终止操作在锁内保持幂等：成功发送终止信号后，重复清理不会再次使用可能已回收的 PID/PGID；首次权限错误继续上报并保留重试能力。调用方仍须等待直接子进程退出并排空管道，不能将发出信号等同于服务已退出。

Windows 可取消媒体命令和受控 ASR 标准化使用 `CREATE_SUSPENDED` 创建子进程，先加入 kill-on-close Job，再通过现有 psutil 恢复执行；恢复失败须回收整个 Job 并保留原错误。该顺序避免短命 FFmpeg 先退出造成接管失败，也阻止接管前创建不受监管的后代。POSIX 独立进程组和 ASR 模型进程的 IPC 启动握手保持原有机制。

Web 启动时的空密码安全提示直接枚举本机 IPv4/IPv6 网卡地址，在线程中执行，不依赖主机名 DNS/mDNS 解析。接口读取失败会记录带异常上下文的警告；API 的本机访问守卫始终独立生效。macOS 的真实 Uvicorn 退出回归同时验证默认空密码启动，不延长原有启动或退出期限。

## 版本与源码冻结

- 应用版本以 [`app/__init__.py`](../../app/__init__.py) 为事实来源。升级时同步包元数据、原生模块、Portable 配置、文档与产物命名，再执行版本一致性检查。模型 revision 和 schema 根据真实契约变化维护，不随补丁版本机械递增。
- **Source Commit** 标识内嵌业务源码快照，**Builder Commit** 标识本次运行构建工具的提交。先提交需要进入 Payload 的业务源码与版本，再更新 Portable 的源码冻结配置；构建工具和文档可以在后续提交维护。
- Payload 构建检查其包含范围内的已跟踪差异和新增未跟踪文件，拒绝工作区业务源码与冻结提交不一致。范围由[源码快照模块](../../packaging/portable/src/blc_portable/payload/source_snapshot.py)定义，不通过忽略检查或手工复制工作区文件绕过。
- 快照通过 Git 提取，构建只校验其自身声明的版本，不在构建期改写旧源码版本。默认重新提取、构建两轮并比较 ZIP SHA-256；原生模块在构建 checkout 中编译，随后复制匹配 ABI 的产物到打包目录。

## 发行契约

- Payload 保存文件清单与 SHA-256，Runtime 在安全解压和验证后通过 staging、锁和原子切换激活；用户配置、数据库、模型和媒体不随业务源码覆盖。具体目录布局见[Portable 构建说明](../../packaging/portable/README.md)。
- 数据库、Runtime、Payload 和模型清单遵循当前明确支持的格式；不能从历史重构记录推断迁移能力。模型安装清单按当前 schema、模型身份与文件内容检查，允许发行来源不同但内容相同的模型复用；在线下载续传记录及构建器仍校验其发行身份。旧 schema 不迁移。
- 真实 Engine Pack 与 fixture 必须明确区分。Fixture 的摘要来自实际构建文件；它只用于验证，不能作为正式模型包分发。主模型、子模型和随附组件使用统一模型目录，保留所需许可证和来源证据。
- 发布清单记录最终文件名、版本、大小、SHA-256 和 CRC32；跨制品检查源码、构建器、Payload 与版本身份。重新构建后的摘要必须重新计算，并检查源码路径、敏感配置、缓存和临时文件未进入产物。

## 可选本地 ASR 集成检查

完成 Engine Pack 安装后，可以显式运行真实模型检查（默认 CPU/int8），不会自动下载模型、调用 LLM 或访问现有业务数据库：

```powershell
python scripts/check_local_asr.py --models-dir "D:/测试安装/models" --audio "D:/测试音频/speech.wav" --engine whisper --expect speech --contains "已知的语音文字" --output .local/local-asr.json
```

`--engine` 可选 `whisper`、`paraformer`、`funasr_nano`；`--device cuda` 用于具备匹配驱动的 GPU 环境；合法静音使用 `--expect no_speech` 并去掉 `--contains`。命令先逐文件重算已安装模型的 SHA-256，再执行生产隔离流水线，核对结果分类、非空语音文本和时间戳，并确认进程回收。输入、依赖、模型或推理异常直接失败，报告不包含转写正文。运行前按当前依赖安装运行环境并提供 FFmpeg；该检查独立于 CI 的确定性回归，CPU 通过不能代替现场 GPU 故障复现。

## 文档约定

发布前运行 `python scripts/check_acknowledgements.py`，并按现有流程执行 pre-commit/CI。README 的“鸣谢”是名单唯一维护入口，紧邻“许可证”之前；检查器使用开发依赖 `markdown-it-py` 解析 Markdown，检查 GitHub 主页 ID、大小写重复和明确退出名单。`StarGazerQQD` 已明确退出鸣谢，不能通过别名重新加入；正常仓库、Issue、PR 链接和许可证版权不受影响。新增反馈者仍须核对实际来源，保留用户手工维护的 Bilibili 等其他平台条目；该检查器只读取文件，不自动收集或改写名单。

- README 提供项目简介、核心功能、快速开始、重要限制和文档入口；详细用法进入对应指南，避免每个版本继续追加历史小节。
- 用户可见变更记入当前系列的 `CHANGELOG.md`；旧系列使用既有 `docs/changelog/` 归档，不重复维护多份版本历史。
- 长期有效的配置说明、架构决策和发行约束留在正式文档，随实现同步维护。一次性任务清单、排查过程和运行日志可留在 PR、Issue 或本地 `.local/` 中，不作为当前使用指南。
- `.local/` 从 Git 跟踪、源码分发包和 Docker 构建上下文中排除。整理历史记录时先提炼仍有效的内容，核对引用，再保留本地原件；不要通过重写 Git 历史删除已经提交的记录。
