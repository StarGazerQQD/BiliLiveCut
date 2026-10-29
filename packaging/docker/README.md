# BiliLiveCut Docker 部署

本目录提供 BiliLiveCut `v0.1.20.2-Alpha` 的 CPU 镜像。构建上下文为仓库根目录；后端容器以 UID/GID 1000 的 `appuser` 运行单实例 API 服务，内含 FFmpeg、中文字体、C/Cython/C++/Rust 扩展及配套 CPU PyTorch/TorchAudio。业务操作见[使用指南](../../docs/features/README.md)。

## 前后端服务

Compose 同时构建后端 `app` 和前端 `frontend`。浏览器访问宿主机 `8000`，由 Nginx 提供 React 静态页面，并同源代理 API 和媒体到仅在容器网络暴露的 `app:8000`。前端镜像在构建阶段使用 Node.js 24，运行阶段只有 Nginx；后端保持 FastAPI，不返回业务页面。

不要额外发布后端 8000 端口。`FORWARDED_ALLOW_IPS=*` 仅用于这个封闭的后端网络，让认证与限流取得前端传递的真实来源；后端单独对外发布时必须收紧可信代理配置。自配 HTTPS 入口时，应让后端接收到正确的 Host、Origin 与协议，保持认证 Cookie 和 CSRF 校验一致。

## 构建成本与资源占用

运行阶段显式安装 Debian 官方安全更新，并检查最低版本：`ca-certificates >= 20250419+deb13u1`、
`libbrotli1 >= 1.1.0-2+deb13u1`、`libexpat1 >= 2.9.0-1~deb13u1`。软件源尚未提供这些版本时构建会失败，
避免沿用基础镜像中仍满足依赖关系的旧包。更新通过重新构建镜像交付，字幕库继续使用 Debian 官方 `libass9`。
最低版本检查覆盖已知修复要求；发布前仍需扫描完整镜像并审查发行版尚未修复的告警。

前端镜像同样安装 Alpine 官方更新：`brotli-libs >= 1.2.1-r0`、`musl`/`musl-utils >= 1.2.6-r3`、
`zlib >= 1.3.2-r1`、`curl`/`libcurl >= 8.23.0-r0`。安装仅在构建阶段使用 root，运行时保持 UID 101；容器验收检查实际用户及包版本。

当前 Compose 会在本机构建完整的 CPU 后端和独立前端。后端需要编译 FFmpeg、PyAV 和原生扩展，并安装 PyTorch、FunASR、faster-whisper 等本地转写依赖；前端需要安装 npm 依赖并构建页面。首次部署需要持续联网，系统包、Python 包、Rust crates 和 npm 包的下载速度都会影响耗时。

以下为本地验收得到的量级参考。实际耗时与占用取决于服务器配置、网络速度、缓存命中情况和工作负载，不作为最低配置或性能保证。

| 项目 | 大致开销 |
| --- | --- |
| 首次完整构建 | 可按数十分钟量级安排等待；网络较慢或 CPU 较弱时可能更久，后端构建通常比前端更耗时 |
| 使用缓存重建 | 复用未变化的构建层可以缩短等待；更新依赖后仍可能需要数分钟或更久 |
| 镜像大小 | 后端约 3 GB、前端约百 MB，未包含 ASR 模型 |
| 构建缓存 | 十余 GB 量级，多次构建和更新依赖后可能继续增长 |
| 构建阶段资源 | 音视频库和原生扩展编译会产生较高 CPU 负载；本地验收观察到 GB 级内存占用 |
| 双端空闲运行 | 容器内存合计约百 MB，CPU 负载较低；Docker 服务另占数百 MB 内存 |

空闲占用来自没有录制任务、没有加载 ASR 模型的环境。功能验收包含转写依赖导入、音视频处理和网页登录，未测量实际模型推理的资源需求。真实录制、转写和渲染时，CPU、内存及磁盘需求会随并发房间数、模型、视频参数和保留时长变化，应按自己的工作量再测量。

较大的依赖主要包括 PyTorch、本地转写引擎及其配套库、Playwright 内部驱动、FFmpeg 和中文字体。编译器、Rust 工具链等只留在构建阶段，生成的中间层和下载缓存仍会占用宿主磁盘。部署时应为镜像和构建缓存预留十余 GB 或更多空间，模型与录制数据另计；镜像与缓存存在共享层，各项逻辑大小不宜直接相加。

### 查看占用与清理构建缓存

在仓库根目录查看本次部署的运行占用和 Docker 磁盘统计：

```bash
docker compose -f packaging/docker/compose.yaml stats --no-stream
docker system df
```

保留缓存可以复用未变化的构建层；依赖锁、构建文件或源代码变化仍可能使后续步骤重新执行。磁盘紧张时，可以交互式清理当前 builder 不再使用的构建缓存：

```bash
docker builder prune
```

该命令作用于当前 builder，可能同时清理其他项目的闲置构建缓存；不会清理应用的 `storage/`。清理后，下次构建可能需要重新下载依赖和编译。模型缓存与录制数据位于下文的持久化目录，应按业务需要单独管理。

## 首次启动

需要 Docker Engine 和支持 `up --wait` 的 Docker Compose v2。在仓库根目录执行：

```bash
test -f .env || cp .env.example .env
mkdir -p storage
# 可在 .env 设置 ADMIN_PASSWORD；留空时通过一次性初始化码在网页设置密码。
# Linux 上确保 storage 可由 UID/GID 1000 写入；不要用 chmod 777。
docker compose -f packaging/docker/compose.yaml up --build -d --wait --wait-timeout 180
```

访问 `http://localhost:8000`，首次进入“设置管理员密码”页。管理员账号固定为 `admin`。
从当前容器日志取得一次性初始化码，设置 12–128 字符密码后登录；支持中文及密码短语，无需记忆随机默认密码。

```bash
docker compose -f packaging/docker/compose.yaml logs --tail=100 app
```

初始化码使用系统随机源生成（192 位），30 分钟内有效、使用后立即失效；只能设置一次，
不能当作管理员密码登录。重启会轮换尚未使用的初始化码，请取当前启动最后一条日志，不要公开日志。
密码以独立随机盐、600,000 次 PBKDF2-SHA256 保存到私有认证库 `/data/config/web-auth.sqlite3`（600）。
保留 storage 后重启/重建仍使用已设密码，不再输出初始化码。认证库丢失或损坏时不降级为免密访问；
损坏时必须由操作者恢复备份，入口不会自动覆盖现有数据。

显式设置 `ADMIN_PASSWORD` 时直接进入登录页，网页不能覆盖该部署密码；修改 .env 后须重建容器，
单纯 restart 不会重新载入 Compose 环境。撤销 ADMIN_PASSWORD 会回到受初始化码保护的设置流程，
不会悄悄恢复先前的网页密码。已有 `admin-password` 随机密码文件会在第一次升级时迁移为哈希，
保持原密码可用，成功落盘后移除旧明文文件；非法、共享权限或链接文件仍会拒绝迁移。

### 登录、退出与恢复

网页右上角账号菜单提供退出和退出所有设备，设置中心提供修改后台密码。默认空闲 30 分钟、最长 12 小时；
受保护的后台刷新也算请求活动，但状态探测不续期，最长有效期始终生效。
可通过 `DOCKER_AUTH_IDLE_SECONDS`（60–86400）与 `DOCKER_AUTH_SESSION_SECONDS`（300–604800）配置。
Cookie 使用 HttpOnly、SameSite=Strict；HTTPS 下加 Secure，不将凭据放入 URL 或浏览器本地存储。
会话令牌只以摘要存储，单个账号最多十个会话；登录会轮换当前令牌，退出可立即撤销。

过期、服务重启或会话被撤销时，在原页面弹出重新登录框，保留未保存草稿，不自动重试保存/上传等操作。
其他标签页更新登录状态后，本页会刷新会话校验信息，但仍须手动重试失败操作。
显式退出返回登录页，退出前请保存草稿。退出和改密不会停止后台录制任务。
网页改密须验证当前密码，成功后所有浏览器重新登录，API 也须使用新密码。
审核员账号权限范围不变，密码仍在 REVIEWER_ACCOUNTS_JSON 中管理。

忘记网页密码时，在服务器运行以下命令，使用输出的新初始化码重新设置，不删除业务数据：

```bash
docker compose -f packaging/docker/compose.yaml exec app python -m app.core.docker_auth reset
```

此命令即时撤销所有会话并使旧密码失效，只适用于网页管理密码。使用 ADMIN_PASSWORD 的部署应修改 .env，
再执行 `docker compose -f packaging/docker/compose.yaml up -d --force-recreate`。
初始化码过期可重新执行重置命令或重启尚未完成初始化的容器。
不要通过删除整个 storage 来重置密码。应用是单实例服务，不支持多个容器共用认证库和业务数据库。

浏览器统一使用会话登录，旧 Basic 凭据缓存不会绕过退出和会话超时。
API 客户端继续支持 Basic Auth（admin 或审核员账号）；浏览器使用 Cookie 会话时，
修改请求还需同源 Origin 和 `/auth/status` 返回的 `csrf_token`（请求头 `X-BLC-CSRF`）。
登录相关 POST 端点要求同源 Origin 和 JSON，所有认证失败均不会执行原业务操作。
初始化码、密码和会话令牌均需保密；五分钟内五次错误尝试会触发该来源 IP 的冷却。

对外访问应使用 HTTPS 反向代理并正确传递 Host 与协议。只信任实际代理的地址，不要随意信任任意
X-Forwarded-* 来源；代理与服务看到的 Origin 必须一致，否则修改请求会被拒绝。
跨站嵌入或跨域网页登录不受支持。DOCKER_AUTH_ENABLED 由镜像及入口强制开启，
源码/Portable 默认仍沿用原有认证方式。

镜像入口也检查目录写权限。宿主绑定目录的权限不会被镜像内 chown 自动修复。可通过调整宿主目录权限解决，或明确执行以下一次性初始化（会改变 storage 内文件的所有者）：

```bash
docker compose -f packaging/docker/compose.yaml run --rm --user root --cap-add CHOWN \
  --entrypoint chown app -R 1000:1000 /data
```

前端容器监听 `8080`，Compose 将宿主 `8000` 映射到前端。FastAPI 在后端容器监听 `8000`，只通过 Docker 内网供前端访问。只需要本机或 SSH 隧道访问时，将前端端口映射改为 `127.0.0.1:8000:8080`。

## 启动、查看和停止

```bash
bash scripts/docker-up.sh
docker compose -f packaging/docker/compose.yaml logs -f app
docker compose -f packaging/docker/compose.yaml ps
bash scripts/docker-down.sh
```

Windows 对应 `scripts\docker-up.bat` / `scripts\docker-down.bat`。脚本可以从任意工作目录启动，命令失败会返回非零，启动脚本等待 Web 健康检查通过才提示成功。HTTP 健康检查直接访问本机服务，绕过外部代理，不读取认证凭据；200 和认证挑战 401 均表示 Web 已能响应，不代表模型推理成功。

Compose 默认移除全部 Linux capabilities，并设置 `no-new-privileges`，阻止进程通过 setuid 程序取得额外权限。目录所有者修复命令只在一次性维护容器中显式授予 CHOWN。Compose 使用 `init: true` 回收子进程，停止宽限为 60 秒，为 FFmpeg 收尾和 Worker 关闭留出时间。重启策略为 `unless-stopped`，手动停止后不会自动重新启动。不要增加 Web workers 或复用同一数据库启动多个容器：当前录制监控和任务调度属于单实例应用。

## 数据与缓存

宿主 `storage/` 挂到容器 `/data`，覆盖数据库、录制、成片、日志、插件和模型缓存：

| 内容 | 容器路径 |
|------|---------|
| SQLite | `/data/blc.db` |
| 录制、成片与日志 | `/data/raw`、`/data/clips`、`/data/logs` |
| 插件 | `/data/plugins` |
| Launcher 运行配置 | `/data/config` |
| 管理员密码哈希和登录会话 | `/data/config/web-auth.sqlite3`（权限 `600`） |
| Hugging Face 缓存 | `/data/cache/huggingface` |
| ModelScope 缓存 | `/data/cache/modelscope` |
| 可选浏览器缓存 | `/data/cache/playwright` |

旧版本 Compose 的 `hf-cache` 卷挂到 `/root`，与 appuser 的缓存位置不匹配；新配置改为 `/data/cache`，不会自动删除或迁移旧卷。重建或删除容器保留宿主 storage，数据库仍为 `/data/blc.db`，路径不随镜像版本改变。当前版本要求 Schema v6、模型指纹、实际结构、约束和完整性全部通过；应用版本仅记录创建来源。升级前停止容器并备份整个持久目录，不兼容格式会保留并拒绝启动，不自动迁移或替换，也不能手改版本绕过校验。

镜像默认 CPU 推理，模型权重仍在首次使用时下载，因此需要足够磁盘、内存和可访问的模型源。镜像构建和 CI 只验证真实 SDK 导入与服务运行，不下载转写模型、不测量真实转写吞吐。GPU 部署需另外准备匹配的 CUDA 镜像和设备透传；修改设备配置为 cuda 不能使这个 CPU 镜像支持 GPU。

## 登录限制

Web、录制及 Bilibili 扫码登录不需要桌面。在“账号登录”页点击“扫码登录”，用手机 Bilibili 扫描网页上的二维码并确认；服务端保存 Cookie，无需安装 Chrome、Chromium 或 Xvfb。扫码后未确认时不会保存登录态，二维码过期后需重新申请，也可点击“取消扫码”。手动填写 Cookie 的方式继续可用。

“浏览器登录”仍会在运行应用的机器打开有界面浏览器，仅适用于有桌面的部署；此镜像不提供远程桌面。

## 构建和依赖更新

构建阶段通过固定 uv 版本，以 `uv.lock` 安装运行 extras，复制完整 setuptools 数据文件和 C/Cython/Rust 构建源，并使用构建阶段的 Node.js 24 在非 editable 安装时生成随包前端资源。运行阶段只复制环境，不携带 Node.js、编译器、uv 或仓库源码；对外页面仍由独立前端容器提供。Rust 仅在构建阶段安装工具链，以 `cargo build --release --locked` 校验现有 Cargo.lock，最终镜像只复制编译后的扩展，随包许可证位于 `/usr/share/doc/bili-live-cut-rust/THIRD_PARTY_NOTICES.md`。传统 pip 源码安装流程不变。

基础系统使用 Debian 13（Trixie）的 Python 3.12 镜像。构建阶段的系统 pip 通过 `requirements-system.lock` 固定版本和哈希；运行阶段移除 pip 及 ensurepip 自带的旧引导 wheel，应用虚拟环境不受影响。容器依赖通过修改锁文件并重建镜像更新，不支持在运行容器内临时执行 pip 或 apt 安装。账号创建完成后，精确移除 apt、libapt-pkg7.0、sqv、mount、passwd、adduser、libsemanage2、libsemanage-common、libsepol2；不自动移除依赖，不强制删除 Debian 必需或受保护包。保留 dpkg 清单供漏洞扫描，也保留证书、时区、现有 UID/GID 1000 账号及媒体和 Python 运行库。容器挂载由宿主 Docker 管理，无需镜像内 mount 命令；后台密码由应用认证库管理，不依赖系统 passwd。派生镜像如需另建系统账号或安装系统包，应在运行镜像裁剪前的构建阶段完成。验收分别检查应用与系统 Python，要求安装工具已移除；系统环境若出现额外第三方包，仍必须逐个审计。

命令行 FFmpeg 与 PyAV 使用同一套从官方 9.0.2 源码编译的共享库，避免 PyAV wheel 另行携带旧版 FFmpeg。源码已核验官方发行签名并固定 SHA-256；完整源码与 GPLv3 许可证随镜像置于 `/opt/ffmpeg/share/source/`。构建保留 CPU 编解码、网络录制、字幕及中文文本滤镜；此无头 CPU 镜像不提供 ffplay 桌面播放器。

字幕渲染恢复使用 Debian 官方 `libass-dev`（构建）和 `libass9`（运行），不再下载 libass 源包或应用外加补丁。FFmpeg 和 PyAV 加载同一套 Debian 共享库，实际包版本由最终镜像的 `dpkg-query -W libass9` 查询。运行镜像保留 libass 官方包的完整文件（包括两份变更日志），验收核对包文件完整性、共享库实际加载路径，并执行中文 SRT 烧录；官方包中的未修复漏洞仍须如实保留在系统包扫描结果中，不能视为已修复。

CPU 运行锁采用官方 Torch 2.13.0 与 TorchAudio 2.11.0 的 CPU wheel；具体兼容范围由官方 wheel 元数据、依赖一致性检查和真实 PCM 解码共同验证。旧 Torch 2.10.0 命中已知漏洞记录，不能继续作为构建基线。

项目 extras 暂未声明 PyTorch，CPU 镜像另外使用 `requirements-cpu.lock` 的官方 CPU wheel、完整配套依赖和 SHA-256 哈希。共享依赖版本来自 uv.lock；最终安装使用 `--inexact` 保留 CPU 运行时，再执行依赖一致性和真实导入校验。

更新项目锁后，在仓库根目录重新生成 CPU 依赖锁并验证镜像：

```bash
mkdir -p .local
uv export --locked --no-dev --extra asr-all --extra llm --extra web \
  --no-emit-project --no-hashes --output-file .local/docker-runtime-constraints.txt
uv pip compile packaging/docker/requirements-cpu.in \
  --constraint .local/docker-runtime-constraints.txt \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match --generate-hashes --no-annotate \
  --output-file packaging/docker/requirements-cpu.lock
uv pip compile packaging/docker/requirements-system.in \
  --python-version 3.12 --generate-hashes --no-annotate \
  --output-file packaging/docker/requirements-system.lock
docker build -t bili-live-cut:docker-test -f packaging/docker/Dockerfile .
docker build -t bili-live-cut-frontend:docker-test -f frontend/Dockerfile .
uv run --no-project --python 3.12 --with pip-audit==2.10.1 python scripts/audit_docker_runtime.py
uv run --no-project --python 3.12 --with playwright==1.61.0 playwright install --with-deps chromium
uv run --no-project --python 3.12 --with playwright==1.61.0 python scripts/smoke_docker.py --browser
```

当前验收目标为 Linux x86_64 / Python 3.12，其他架构应重新生成对应锁并完成实际构建验收。Docker 工作流执行实际镜像构建与隔离容器检查，包括目录权限失败、显式密码、一次性初始化、网页登录和重登、CPU ASR 导入、原生扩展、FFmpeg 滤镜、中文字体、数据及密码持久化以及停止后重建。验收使用临时卷，不读取仓库 .env，也不连接真实直播或下载模型。

基础 Python 和 uv 镜像按已验收的 SHA-256 digest 固定，新增 Action 使用完整提交。更新镜像时应核对官方来源并重新运行构建、依赖审计和容器验收，不能只移动标签。

Docker CI 分别从最终镜像的 `/app/.venv/bin/python` 和 `/usr/local/bin/python` 提取实际安装包清单，包含 Linux 专属依赖和 CPU Torch，并核对系统安装工具已移除；只在查询公共漏洞记录时将官方 Torch/TorchAudio 的 `+cpu` 映射到相同公共版本。原始清单与完整报告分别保留在 `.local/docker-audit/application/` 和 `.local/docker-audit/system/`；系统无第三方包时只记录清单检查及未执行漏洞查询，不生成 pip-audit 报告。未知本地版本、包被跳过、版本不符、报告不完整、查询失败或任一已知漏洞均阻断验收，不采用漏洞忽略名单。应用自身通过源码审计和测试验收；此检查不等同于 Debian 系统包扫描，也不证明任意外部模型或插件可信。

### 构建时的源码下载

FFmpeg 9.0.2 优先从官方 HTTPS 地址下载；传输失败后使用 [MacPorts 官方源码缓存](https://distfiles.macports.org/ffmpeg/)。两者必须匹配原有固定 SHA-256 `8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e`，对应同一份发行源码；不使用镜像提供的补丁或二进制。

每个来源最多重试 2 次，连接超时 20 秒，单次传输超时 120 秒；两个下载子进程各有 270 秒硬上限，合计不超过 540 秒。仅网络及 HTTP 下载失败可以切换来源。摘要不符、证书验证失败、配置或本地文件错误直接中止；两个来源均不可达也会阻断构建。临时文件通过真实摘要校验后才原子替换目标归档，后续才会解压和编译。curl 和下载脚本只用于构建阶段，不进入运行镜像。
