# 选择安装方式

BiliLiveCut 的完整流程是：添加直播间 → 录制或导入视频 → 转写与识别高光 → 人工审核 → 生成切片 → 可选上传。先选部署方式，再准备实际需要的模型和服务。

## 安装入口

| 运行环境 | 建议入口 | 需要准备 |
| --- | --- | --- |
| Windows 桌面 | [Portable 使用说明](../../packaging/portable/USER_GUIDE_ZH.md) | Full 自带 Python 和 FFmpeg，模型另行准备 |
| Linux 无头服务器 | [Linux 部署指南](linux-server.md) | Python、FFmpeg；原生扩展构建需要编译工具和 Python 开发头文件 |
| Docker | [容器部署](../../packaging/docker/README.md) | Docker，镜像与构建缓存空间，持久化数据目录和模型空间 |
| Windows、macOS 或 Linux 源码运行 | [源码部署](source.md) | Python、FFmpeg；默认原生扩展安装需要编译工具和 Python 开发头文件 |
| 修改代码或参与开发 | [开发环境](../development/environment.md) | uv 或 venv + pip、FFmpeg、编译工具；JavaScript 检查和文档站另需 Node.js |

源码安装同时支持 uv 和传统 venv + pip。Web 控制台使用独立 React 前端：源码安装需要 Node.js 24 构建页面；wheel、Docker 和 Portable 自带静态产物，运行时不需要 Node.js。文档站使用独立的依赖与构建。

当前 Docker 部署会在本机构建包含本地 CPU 转写依赖的完整镜像，首次构建耗时和磁盘占用较大。选择前请阅读[构建成本与资源占用](../../packaging/docker/README.md#构建成本与资源占用)，为构建缓存、模型和录制数据分别预留空间。

## 第一次运行

1. 启动 Web 服务，打开终端给出的地址。
2. 根据部署方式完成管理员登录或首次初始化。管理员账号与 Bilibili 账号是两套独立的登录。
3. 在“Bilibili 登录”页面扫码。无头服务器通过浏览器显示二维码，无需服务器桌面。
4. 添加一个直播间，进行短时间录制；已有视频可直接使用[录播导入](../features/recording-import.md)。
5. 准备所选 ASR 模型和分析服务，再测试转写、高光识别、审核与渲染。

只有录制和 Web 管理需求时，可以先不安装 ASR/LLM 可选依赖。完整本地转写需要对应依赖、模型文件及足够的内存与磁盘空间，首次模型下载可能耗时。

## 接下来读什么

- 日常操作、CLI、转写与排错：[使用指南](../features/README.md)。
- 参数含义、常用/高级设置与生效时间：[配置参考](configuration.md)。
- 修改与检查 Web 页面：[Web 页面开发与验证](../development/frontend.md)。
- 二次开发与平台扩展：[插件开发](../../plugin/README.md)、[直播源插件](../development/live-source-plugins.md)。

## 升级前保留旧环境

升级前停止录制和服务，备份完整运行目录。应用版本只记录数据库创建来源；格式版本、模型指纹、实际表列、唯一约束、外键及完整性全部通过时，新程序可以复用原数据库，且不改写来源版本。不兼容格式会拒绝启动，须先在副本上验证受支持的转换方案，或显式配置独立新库；不能手改版本或指纹绕过校验。

Portable 仍以 Launcher 所在目录作为运行目录，版本化 `runtime/releases/` 只存源码；Docker 仍使用持久卷中的 `/data/blc.db`。源码部署更换工作目录时，应显式核查 `DATABASE_URL` 与 `STORAGE_ROOT`，相对路径随工作目录解析，不会自动寻找其他目录的数据库。独立测试请使用数据库及媒体副本，避免两个版本同时写同一库；降级还需核查目标版本的配置与业务记录兼容性。模型内容按现有完整性规则复用，详见[项目说明](../../README.md#使用与升级注意事项)。

在已停止服务并保存的完整副本上运行 `blc storage-check --database "副本/storage/blc.db" --storage "副本/storage"`。命令逐项列出缺少数据库场次的目录、身份冲突、登记缺失的原片、找不到文件的分段和早于开场的弹幕数量。退出码 1 表示需要复核。已按保留策略清理的原片也可能列为文件缺失。旧场次没有新身份标记时，需结合旧库、原片及录制日志确认归属；发现冲突后保留两份原件，勿删除补做记录或再次覆盖原库。

制作副本时，一并保留同一时点的 `blc.db-wal`、`blc.db-journal` 等同名数据库文件（如果存在）。如有非空日志，检查命令会在临时目录复制主库和日志，读取已提交的数据后清理临时副本。临时目录需容纳这些文件。

如使用独立的外置目录，可在命令末尾添加 `--map-dir "F:\BiliLiveCut\raw" "备份盘上的原片副本"`。每个外置目录分别提供映射，包含已经切换掉但仍保存历史场次的目录。未提供映射的外置位置会列为待核对。

新录制和本地导入会跳过 `raw`、`clips` 和 `ready_to_upload` 已占用的场次目录。新建的 `raw/session_<编号>/.blc-session-id` 需要随数据库和素材一起备份。编号有空缺属于正常情况。

### 从历史文件生成新库

管理员可在 Web 设置的“存储与清理”下展开“从历史文件重建媒体库”，逐个添加服务主机上的来源目录。每个来源可配一份已停写的数据库副本，也可仅填写存储目录。作业完成后会显示新库路径、场次数、原片数、弹幕数和缺失文件数。

命令行使用同一套重建逻辑。输出应为已有父目录下尚不存在的新目录，例如：

```shell
blc storage-rebuild --source "D:/旧版/storage/blc.db" "D:/旧版/storage" --source "F:/备份/blc.db" "F:/备份" --output "D:/恢复/合并库"
blc storage-rebuild --files "D:/保留的storage" --output "D:/恢复/文件索引"
```

使用外置目录时，可用 `--manifest 来源.json` 替代 `--source` 或 `--files`。例如 `{"sources":[{"storage":"D:/旧版/storage","database":"D:/备份/blc.db","raw":"F:/原片","danmaku":"G:/弹幕"}]}`。路径均以服务主机为准。

若旧库使用绝对路径且整个 storage 已搬迁，在该来源填写 `original_storage` 指明原根目录。先后使用过多个外置位置时，用 `directory_maps` 逐一对应，例如 `{"F:/旧原片":"K:/备份一","G:/旧原片":"L:/备份二"}`。同一来源的场次分布在多个根目录时，将单一原片或弹幕目录留空，让每个场次沿用自己的登记。

支持当前 Schema 6 及上一版 Schema 5。新库单独分配编号，保留场次与原目录的对应关系。旧库可提供转写、候选、成片和发布历史；只有文件时，可恢复场次文件、可读取的弹幕和分段清单中的时长。缺少弹幕首行时间时，文件索引以文件修改时间作为参考，并记录时间来源。原档重新解析出的弹幕数量可能与过去的采样数量不同。

核对新库后，可在独立环境中通过 `DATABASE_URL` 指向新生成的 `blc.db`。原素材应保留在已登记的位置。重建不会切换运行中的服务，也不会接续旧库的录制、分析或上传作业。

## Python 依赖源

默认可使用官方 PyPI。需要镜像时，现有源码指南提供以下 PowerShell 进程级配置；这些变量用于 pip 安装，不写入应用 `.env`：

```powershell
$env:PIP_INDEX_URL='https://mirrors.aliyun.com/pypi/simple/'
$env:PIP_EXTRA_INDEX_URL='https://pypi.tuna.tsinghua.edu.cn/simple/'
```

依赖仍以当前版本的 `pyproject.toml` 与 uv 锁文件为准。原生扩展的编译器和头文件准备见[开发环境](../development/environment.md#原生扩展构建与安装排错)。
