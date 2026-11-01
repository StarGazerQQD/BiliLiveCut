# Web 页面开发与验证

业务控制台使用 React 19、TypeScript、Vite、Tailwind CSS 4 和 shadcn/ui。React Router 管理页面，TanStack Query 管理服务端读取与缓存，API 类型通过 openapi-typescript 从 FastAPI 的 OpenAPI 生成。文档站仍使用 VuePress，两个前端独立构建。

## 页面与后端边界

浏览器只访问前端地址。前端服务提供 `/`、工作台路由和静态资源，并把 `/api`、`/auth`、`/review/api`、`/collection/api`、`/openapi.json` 反向代理到 FastAPI。FastAPI 负责 API 与媒体响应。

| 路径 | 职责 |
| --- | --- |
| `frontend/src/pages/` | 直播、导入、审片、合集、设置与运维页面 |
| `frontend/src/components/ui/` | shadcn/ui 基础组件 |
| `frontend/src/api/` | 认证、请求、OpenAPI 类型 |
| `frontend/src/lib/drafts.ts` | 当前浏览器内存中的编辑草稿 |
| `frontend/public/media-auth.js` | 为同源媒体请求附加当前标签页的 Basic 凭据 |
| `app/web/main.py`、`app/web/routers/` | 后端 API 与媒体 |
| `app/web/frontend_server.py` | 源码、wheel 及 Portable 使用的独立静态服务进程 |
| `frontend/nginx.conf` | Docker 前端的静态服务与反向代理 |

React 录播页面支持视频与弹幕多选，在请求前完成同名配对、歧义校验和自然排序。当前标签页保存每段声明和完成状态；失败或停止后继续同一队列，先核对服务端已上传状态，再上传剩余文件。相关行为由 `import-plan`、`import-controller` 单元测试和真实浏览器的批量导入测试共同覆盖。

## 生产请求边界

Python 独立前端默认忽略客户端提供的转发头，后端认证与限流使用 Uvicorn 按可信连接确认的来源。经外层 HTTPS 代理部署时，在部署环境设置 `FRONTEND_TRUSTED_PROXY_IPS`，例如 `127.0.0.1/32,::1/128`，并重启前端。只填写真实外层代理的地址；可信代理必须重新生成 `X-Forwarded-For` 和单一的 `X-Forwarded-Proto`，保留外部 Host。此配置属于部署参数，网页不能保存，且与后端的 `FORWARDED_ALLOW_IPS` 分别配置。默认空值不接受外层转发头。

Docker 使用独立 Nginx 配置，此变量仅作用于 Python 前端。需要在容器外终止 TLS 时，部署的 Nginx 配置应只信任实际外层代理，并将确认后的来源和 HTTPS 协议传给后端；不能直接透传任意客户端声明的转发头。后端保持私有网络，公开部署继续启用认证。

普通管理请求最多 2 MiB，登录请求最多 4096 字节。转写和房间编辑的指定 PATCH 端点允许 4 MiB，模型目录保存和测试端点允许 8 MiB，以容纳现有字段容量及 Unicode JSON 转义开销，业务字段限制保持有效。视频和弹幕的指定 PUT 上传端点分别保留 50 GiB 和 32 MiB 上限；ASS 文件内容最多 1 MiB，整体请求另留 multipart 开销。前端检查声明长度和 chunked 累计量，后端在解析前检查实际读取量，超限返回 413。

生产静态服务发送 CSP：脚本、API、字体和 Worker 使用同源资源，媒体允许同源和 blob，图片允许同源、data 和 blob。保留 React 所需的内联样式，不允许内联脚本、对象嵌入或外部页面嵌入。Nginx 和 Python 前端保留资源 MIME、Service Worker 和缓存行为；缺失资源不使用长期 immutable 缓存。

## 本地修改与刷新

准备 Python 3.12 和 Node.js 24。下面的命令都从当前工作树根目录执行；使用 Git worktree 时，每个工作树需要自己的 Python 环境。

首次运行先安装依赖（包含 Web、Whisper CPU 转写和 LLM 客户端，不下载转写模型）：

```bash
uv sync --locked --no-dev --extra web --extra asr-whisper --extra llm
```

如果终端激活了另一个工作树的虚拟环境，先执行 `deactivate`。不要使用 `--active` 指向旧环境。缺少 `Python.h` 或编译工具时，可以在安装命令前添加 `BLC_SKIP_C_EXTENSIONS=1`，使用 Python 实现运行；完整开发依赖及其他转写引擎见[开发环境](./environment.md)。

`--no-sync` 只适用于已经安装好依赖的环境，不会补装 `click` 等缺失依赖。先完成上述安装，确保当前工作树的 `.venv` 中已有 `blc` 入口，再启动后端：

```bash
uv run --no-sync blc backend --host 127.0.0.1 --port 8000 --reload
```

另开终端，进入同一个工作树根目录：

```bash
cd frontend
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`。Vite 对 React、CSS 修改提供热更新；`--reload` 只重启 Python。开发代理默认指向 `127.0.0.1:8000`，更换后端端口时设置 `BLC_API_PROXY`。Vite 会保留原始 Host，写操作的 Origin 与 CSRF 继续由后端验证。

后端重载只监听源码目录下的 `app/` 和 `config/`，修改打包脚本或测试文件不会触发重启。数据库的应用版本仅记录创建来源；格式、模型指纹、实际结构、约束和完整性全部相符时可复用。升级前停止服务并备份运行目录；不兼容格式会拒绝启动，先在副本上核查转换方案，禁止手改元信息绕过校验。

默认从当前工作树读取 `.env`，数据写入该工作树的 `storage/`。独立测试使用数据库和媒体副本，避免不同版本同时写同一库；Cookie 等凭据需按当前配置核查，不随代码分发。单独设置运行目录时，需要同时设置存储和数据库路径，不能仅设置 `BLC_APP_ROOT`。编辑草稿与 Basic 密码只保存在当前页面内存，跨页面导航保留草稿；整页刷新或关闭后不会恢复。Docker 使用 HttpOnly 会话 Cookie，写操作携带 CSRF，过期后保留草稿并要求重新登录，不自动重放写操作。

## 构建与运行

安装后可执行文件名为 `blc`，由 Click 提供命令解析：

| 命令 | 运行内容 | 默认地址 |
| --- | --- | --- |
| `blc backend` | 仅 FastAPI 后端，可加 `--reload` | `127.0.0.1:8000`，或保存的启动端口 |
| `blc frontend` | 仅静态前端，代理到 `--backend` 指定的后端 | `127.0.0.1:8080` |
| `blc start` | 同时启动已构建前端和私有后端，统一管理退出 | `127.0.0.1:8000`，或保存的启动端口 |

源码运行 `frontend`、`start` 前需要生成 `frontend/dist/index.html`。wheel 使用随包的 `app/web/ui/`，Portable 使用 Payload 中的 `frontend/dist/`；也可通过 `--frontend-dir` 或 `BLC_FRONTEND_DIR` 显式指定目录。运行命令不调用 npm，不提供前端开发热更新。`serve`、`console` 分别作为 `backend`、`start` 的兼容别名。录制、转写、剪辑、上传、日程、配置和维护命令使用 `blc --help` 或 `blc <命令> --help` 查看。

```bash
npm --prefix frontend run build
uv run --no-sync blc start --host 127.0.0.1 --port 8000
```

`start` 等待 API 启动后，启动另一个进程提供静态页面并代理 API；任一进程退出时会关闭另一个进程。运行构建产物不需要 Node.js。也可以分别运行两个服务：

```bash
uv run --no-sync blc backend --host 127.0.0.1 --port 8000
# 另一个终端，浏览器访问 8080
uv run --no-sync blc frontend --port 8080 --backend http://127.0.0.1:8000
```

对外服务推荐 Docker 中的 Nginx，或配置自己的 HTTPS 静态服务与反向代理。后端端口应保持内网访问；Basic 模式下，远程浏览器的媒体认证使用 Service Worker，需要 HTTPS 或 localhost 安全上下文。使用 SSH 隧道时访问本机 localhost 即可。

Basic 媒体请求只向发起请求的同源标签页，通过独立消息端口读取该页面内存中的凭据。Worker 不缓存密码，浏览器回收并重新启动 Worker 后可继续播放；退出或认证失效后的新请求不再附加凭据。页面只回应当前控制它的同源 Worker。页面已关闭或在 1.5 秒内无法回应时，保留原请求交由服务端认证，不借用其他标签页的登录状态。Docker 的 HttpOnly Cookie 会话仍由浏览器随原请求发送。

## 语法与交互检查

```bash
npm --prefix frontend ci
npm --prefix frontend run check
npm --prefix frontend exec playwright install chromium
npm --prefix frontend run test:e2e
```

检查包含 TypeScript、lint、Vitest 和构建；浏览器测试先构建生产前端，再使用独立预览服务覆盖桌面与手机导航、空列表、设置计数、草稿保留、认证失效等行为，避免开发服务器的依赖预构建干扰验收。代理测试使用真实 HTTP 请求验证流式上传、Range、Cookie、CSRF、缓存和路由回退。

CI 与 Release 的共享提交检查均先准备 Node.js 24，再通过 `npm --prefix frontend ci` 按锁文件安装依赖。Release 的 Payload 作业单独准备 Node.js 24；Payload 构建器在冻结的源码快照中安装前端依赖并构建，不依赖其他作业的 `node_modules` 或 runner 自带的 Node。

后端接口变化后，在已安装 web 依赖的 Python 环境执行：

```bash
uv run --no-sync python scripts/export_openapi.py
npm --prefix frontend run api:generate
```

将 `frontend/openapi.json` 和生成的 `src/api/schema.d.ts` 一并更新。CI 使用 `--check` 检查规范是否同步，再重新生成类型验证差异。导出脚本使用临时配置目录，不启动应用生命周期，也不会读取正在运行实例的数据库。

## wheel 与源码包

构建 wheel 的机器需要 Node.js 24、npm 及 Python 构建工具：

```bash
uv sync --locked
uv run --no-sync python -m build
```

构建器在临时目录执行 `npm ci` 和 `npm run build`，将完整静态产物放入 wheel 的 `app/web/ui/`，不复用工作树的 `frontend/dist/`，也不改动开发环境的 `node_modules`。sdist 包含前端源码和锁文件；从 sdist 构建 wheel 时执行相同流程。缺少 npm 或前端构建失败时，wheel 构建直接失败。

安装已构建的 wheel 后，使用 `blc start` 从任意运行目录启动，运行时不需要 Node.js。editable 安装（`uv sync`、`pip install -e`）不自动构建页面，仍按上文使用 Vite 或手动生成 `frontend/dist/`。

Release 会在源码目录之外安装 wheel，再用真实 `blc start` 检查首页、脚本、样式、SPA 路由、API 代理及进程退出；验收运行时的 PATH 不提供 Node.js。

## 功能与验收范围

页面入口覆盖直播间和房间自动化、录制控制和预约、录播导入及恢复、转写校正、弹幕证据与归档、整场时间线和总结、多人审核及边界调整、主题合集、成品和投稿、网感、流水线与后台作业、分析、模型与磁盘状态、日志，以及所有运行参数、服务商、插件、账号和模板。

验收仍需使用真实媒体、已准备的 ASR 模型和可用的服务配置，完成一次录制或导入到审核渲染的链路。模拟浏览器测试不会证明平台登录、取流、模型推理或投稿已经成功。
