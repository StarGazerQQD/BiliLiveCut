# 直播源插件契约

来源契约版本：`app.plugins.live_source.LIVE_SOURCE_API_VERSION == "1"`。
宿主插件 API 仍为 `1`，本接口自应用发布版本 `0.1.18.4-alpha` 提供。加载时仍须检查契约版本：
插件清单必须同时声明 `capabilities: ["live_source"]` 与 `live_source_api_version: "1"`。
旧宿主的严格清单校验会拒绝未知能力/字段，新宿主会拒绝缺失或不支持的来源契约版本。
既有 `highlight_scorer` 清单不需要变化。

## 公共模块

稳定导入路径为 `app.plugins.live_source`；生命周期、设置使用 `app.plugins`。
插件无需导入数据库模型、录制器或来源注册器。

```python
from app.plugins import BasePlugin, PluginContext, PluginSetting
from app.plugins.live_source import (
    LiveSource, SourceDescriptor, SourceRoom, RoomSnapshot, LiveStatus,
    StreamPreference, StreamSpec, SourceTemporaryError, SourceRateLimited,
    SourceAuthenticationError, SourceInvalidInput, SourceUnavailable,
)
```

`LiveSource` 必须提供 `descriptor: SourceDescriptor` 和四个异步方法：

```python
async def resolve_room(self, value: str) -> SourceRoom: ...
async def get_room_info(self, room: SourceRoom) -> RoomSnapshot: ...
async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]: ...
async def aclose(self) -> None: ...
```

模型拒绝未知字段，顶层不可变。`StreamSpec.headers` 在宿主使用前重新校验。

| 模型 | 字段和语义 |
| --- | --- |
| `SourceDescriptor` | `platform`：小写稳定平台标识，`local` 保留；`name`：显示名；`domains`：精确小写主机名元组，包含短链接域名，不接受通配符、端口、路径 |
| `SourceRoom` | `platform`、严格字符串 `source_id`、HTTP(S) `canonical_url`；ID 不是宿主房间主键，也不能是随开播变化的场次 ID；规范地址不得含凭据、查询串或 fragment |
| `RoomSnapshot` | `status` 为 `LiveStatus.LIVE/OFFLINE/UNKNOWN`；`title`、`uploader_name` 缺失时为 `None`；`observed_at` 默认 UTC 当前时间，显式传入必须带时区 |
| `StreamPreference` | `intent`：`best/balanced/data_saver`，默认 best；`preferred_transport`：`hls/flv`，默认 hls；平台负责解释和排序，宿主不比较平台清晰度数字 |
| `StreamSpec` | `url` 临时 HTTP(S) 地址；`transport`：hls/flv；`container`：ts/fmp4/flv；`headers` 字典；可选 `quality_id`、`quality_label`、`codec`、带时区的 `expires_at` |

来源将候选按偏好从高到低排序，每次取流返回新凭据。过期候选被排除，空列表表示没有可用播放候选，
不能据此断言下播。Bilibili 适配器继续使用宿主原有 `stream_quality` qn 和协议偏好，仅在 Bilibili 内比较 qn。
来源 URL 必须是 HTTP(S)，不支持本地文件、shell 命令或自定义 FFmpeg 参数。
请求头拒绝控制字符、大小写重复键、Host、代理鉴权和消息分帧控制头。播放凭据不应出现在异常消息或日志中。

## 注册与生命周期

```json
{
  "id": "my-live-source",
  "name": "我的直播源",
  "version": "1.0.0",
  "api_version": "1",
  "live_source_api_version": "1",
  "entrypoint": "main.py:Plugin",
  "capabilities": ["live_source"],
  "settings_page": true
}
```

插件在 `on_enable(context)` 内构造来源对象，调用 `context.register_live_source(source)`，可以多次调用。
方法只暂存对象：启用钩子成功后整组原子注册，平台和域名冲突都会拒绝，内置 Bilibili 不能覆盖。
宿主接管已提交给上下文对象的 `aclose()`，启用失败也会关闭暂存来源，然后调用插件 `on_disable()`。
插件不得在钩子结束后继续注册。没有提交给上下文的资源仍由插件自己负责释放。

停用先标记来源不可接受新任务，等待宿主使用者停止及片段收尾，取消在途请求，再 `aclose()`、
`on_disable()`、卸载模块。若使用者未退出，不卸载仍被使用的插件。`aclose()` 必须幂等。
启用状态和命名空间设置沿用现有插件管理器，服务重启自动恢复已启用插件。

插件设置示例：`PluginSetting(key="cookie", label="平台 Cookie", kind="password")`。
来源通过保存的 `PluginContext.get_setting("cookie", "")` 读取自己的设置；禁止读取 Bilibili Cookie。
密码设置隐藏回显但在本地数据库中存储，按现有插件模型信任插件代码。

## 错误、并发和资源

错误类均继承 `SourceError(RuntimeError)`，支持 `retry_after: float | None`（非负有限秒数）：
`SourceTemporaryError` 暂时故障、`SourceRateLimited` 限流、`SourceAuthenticationError` 认证失效、
`SourceInvalidInput` 输入错误、`SourceUnavailable` 未启用/不可用。
异常消息必须可公开；用 `raise ... from exc` 保留原始原因，禁止把签名 URL、Cookie、token 放进消息。
查询失败抛异常；确实无法判定状态返回 UNKNOWN；只有明确下播返回 OFFLINE。

宿主每来源最多同时进行 4 次短请求，默认每次 10 秒、暂时故障最多 2 次尝试，间隔至少 0.25 秒。
限流立即返回并对该来源等待 `retry_after`（缺省 60 秒）；长退避交回后续调度。
认证错误不重试，未知插件异常转换为不包含原异常内容的不可用错误。调用者取消会传播给来源。
启用/停用钩子默认限时 30 秒，来源关闭默认 10 秒。
来源方法必须协作取消，不能吞掉 `CancelledError`；Python 无法强制终止不协作的进程内插件。
同步 SDK 应使用 `asyncio.to_thread` 或受控执行器，同时设置 SDK 自身 I/O 超时；线程取消不会中止底层阻塞调用。
插件内部不得自行启动无限重试或第二套录制、调度流程。

## 可选弹幕契约

完整可选签名和回调类型如下，来源对象实现该方法后宿主自动识别能力：

```python
from collections.abc import Awaitable, Callable
from app.plugins.live_source import SourceRoom, DanmakuEvent, DanmakuStatus

DanmakuSink = Callable[[DanmakuEvent], Awaitable[None]]
DanmakuStateSink = Callable[[DanmakuStatus], Awaitable[None]]

async def collect_danmaku(
    self, room: SourceRoom, emit: DanmakuSink, state: DanmakuStateSink
) -> None: ...
```

`DanmakuSource.collect_danmaku(room, emit, state)` 为独立可选异步长任务协议。
`emit` 接收 `DanmakuEvent(occurred_at, content, user_name=None)`，时间必须带时区，统一转 UTC。
跨平台公共事件只表示普通文本计数，不直接接收礼物金额或平台人气。
宿主区分 `DanmakuStatus` 的 unsupported、disabled、connecting、available、failed。
采集器只能向 `state` 报告 connecting、available、failed；是否支持和是否启用由宿主决定。
available 必须在实际连接完成后报告，零条事件仍可 available；采集暂时失败与不支持能力不同。
宿主控制任务取消；方法退出前应关闭连接。来源自身 `aclose()` 还须能够幂等释放残余资源。

## 契约测试

在隔离检出及已安装开发依赖的 Python 环境运行：

```text
python -m pytest tests/unit/test_live_sources.py tests/unit/test_plugins.py tests/unit/test_highlight_plugins.py
```

测试通过真实插件加载器验证清单、注册、停用、重启恢复及初始化失败回滚；网络边界使用替身。
此契约验收不代表真实抖音直播已经联调。

## 房间登记与平台识别

Web 的添加直播间表单列出已启用来源，可以自动按域名识别，或显式选择平台。
`GET /api/live-sources` 返回相同能力列表；`POST /api/rooms` 接收
`{"url": "直播地址", "authorized": true, "platform": null}`。
`platform` 可省略，纯数字始终默认 Bilibili；不明地址提示安装插件，不会挨个平台试探。
显式平台与 URL 域名不一致会拒绝。短链接域名须在描述符中声明，由插件在有限超时内解析重定向；
返回规范地址必须仍属于该来源的注册域名。插件须为短链、完整 URL 和裸 ID 返回同一个稳定身份。

CLI 同样初始化已启用插件、调用来源、等待清理后退出：

```text
python -m app.cli add-room https://live.bilibili.com/123 --authorize
python -m app.cli add-room ROOM_STRING --platform myplatform --authorize
python -m app.cli check ROOM_STRING --platform myplatform
python -m app.cli list-rooms
```

`check` 只显示状态、传输/容器/编码/清晰度，不输出播放地址或请求头。
登记不会开启 auto_record、auto_analyze、auto_render、auto_approve、auto_upload。
重复登记更新资料和授权，保留同一 DB 房间、历史及自动化选择。
插件停用或缺失时，房间和来源 ID 继续显示，并明确提示来源不可用。
历史候选、任务、场次等界面使用同一来源身份；Bilibili 和本地录播原有标签保持兼容。

## 数据兼容与回滚

Schema 版本保持 `5`，应用版本与模型指纹校验原样执行。本次无表、字段、索引变更，无数据库迁移命令。
应用版本升级仍遵循现有严格策略：`0.1.18.4-alpha` 不直接打开 `0.1.18.3-alpha` 的数据库。
升级请保留旧程序、数据库和媒体，在独立目录初始化新版本；回滚使用原版本与其原数据库。
下述身份兼容指同一应用版本数据库内的既有房间，不表示支持跨版本数据库迁移。
沿用已有 AppSetting 业务元数据方式，在创建/更新 LiveRoom 的同一事务中保存两份严格版本化绑定：
`source_identity:["platform","source_id"]` 为唯一正向键，`source_room:DB_ID` 为反向键；两者内容均为
`{"version":1,"room_db_id":DB_ID,"room":{"platform":...,"source_id":...,"canonical_url":...}}`。
正向键保留完整平台和字符串身份，不使用截断、哈希或伪造整数房号。
SQLite 在读取查重结果之前取得写锁；持久化在工作线程完成，失败整个事务回滚。
正向主键防止并发重复登记；反向绑定和数据库房间关联在读取时复核，损坏则明确拒绝，不自动修复或覆盖。

既有 Bilibili 房间直接从其原数字 room_id 读取来源身份；首次再次登记时补充绑定，保留数据库主键和历史。
非 Bilibili 的旧整数字段 room_id 保持 None，字符串 ID 只存来源绑定。
同一规范地址返回不同稳定 ID 会被拒绝，避免插件解析错误生成重复房间。
普通设置中心的修改/重置不会触碰来源绑定，插件卸载也不删除绑定。

备份时先停止服务，备份完整 SQLite 数据库及关联媒体目录；不复制正在写入且未协调的 WAL 数据库文件。
恢复必须成套恢复相应数据库与媒体。回滚宿主前先停用外部来源并完成在录收尾；旧宿主拒绝新能力清单，
旧版不支持操作外部来源房间。无需删表、删房间、重置指纹，也没有对真实用户数据库执行迁移。

身份和入口回归：

```text
python -m pytest tests/integration/test_source_rooms.py tests/integration/test_web.py tests/unit/test_cli_basic.py tests/unit/test_security_and_db.py tests/integration/test_review_workflow.py
```

## 统一录制、监控与场次证据

手动录制、开播监控、预约和中断恢复均调用同一个来源注册器和 Recorder，
不再要求 `LiveRoom.room_id` 是整数或非空。CLI 使用 `python -m app.cli record DB_ID --pipeline`；
来源插件的初始化、录制和关闭在同一事件循环完成，取消也会进入片段和连接收尾。
开录前与录制中按既有设置刷新标题；场次标题快照、观测变化和结束冻结沿用现有机制。
未知状态及查询失败不会计入下播确认，也不会解除“等待下一次开播”；延迟停录会再次查询明确下播，
并在房间控制锁内核对原录制任务，不能停止后来新开的场次。每个平台的监控任务独立轮询。

Recorder 持有来源直到末片入库、弹幕关闭和结束回调完成。停用先阻止新请求，发停止信号，
最多等待 30 秒，再强停 FFmpeg 等待 5 秒，必要时取消任务并再等待 5 秒；仍未收尾就拒绝卸载来源。
每次重连重新调用 get_streams，已过期候选不使用。临时错误使用既有连续重试次数/时长预算，
实际产出片段后重置预算；认证、输入和来源不可用错误结束本场，保留安全错误码供处理。
此类永久取流错误会持久化自动重启限制，避免监控反复创建失败场次；重启宿主也保留限制。
处理平台设置或来源问题后，手动启动录制或重新开启自动录制可解除限制，原自动审核和上传开关不变。
FFmpeg 仅接受宿主构造的参数数组及 HTTP(S) 协议白名单；播放请求头来自本来源。
临时 URL、Cookie 和请求头不写普通场次字段，不打印 FFmpeg 参数或原始 stderr。
`session_stream:SESSION_ID` 只保存平台、稳定来源 ID、传输、容器、清晰度字符串和编码描述。
原 `RecordingSession.quality` qn 字段保留供历史读取，新场次统一使用上述字符串描述，Bili 取流 qn 设置不变。

`session_danmaku:SESSION_ID` 保存版本 1 的实际连接区间、状态、当场延迟补偿和结束时刻。
连接中每 5 秒持久化观测截止时间；崩溃遗留区间最多认可到最后一次持久观测，重启后标记中断，
不能把停机时间当作持续采集。一次断连不会抹掉此前成功连接的区间。
分析窗口缺少完整覆盖时省略弹幕评分特征、情绪特征和全零解释；热点桶相应指标为 None。
确认连通且零事件则是有效零样本。旧直播场次没有连接记录时只认可已存事件，不由当前全局开关推断。
本地导入仍以 has_comments 区分缺失文件和有效空文件。
Bili 内置会话适配器保留匿名兜底、礼物/SC/进场类型、原有采样及权重；外部公共事件每条计为 1，
时间是平台事件的 UTC 时刻，不叠加 Bili 的接收延迟。不同平台的原始金额或热度不得作为该计数。

外部弹幕采集异常不会阻断录制与 ASR。宿主对暂时失败最多尝试 3 次，等待至少 1/2 秒或 retry_after；
其它错误停止弹幕任务，保留失败状态。采集器必须在取消返回前关闭连接并等待已开始的阻塞 I/O 结束，
不得留后台线程继续发送事件。高光评分请求新增可选 danmaku_available、source_platform，旧插件字段保持兼容。
宿主已开始的弹幕写入及录制收尾会等待完成，连续取消不会提前释放来源。
高光插件的弹幕时间戳与分析窗口按当场延迟统一对齐；外部来源延迟为零，Bili 使用当场固定补偿。
预约和恢复使用房间自身 auto_analyze/auto_render，不由全局 Pipeline 默认值重新开启。
来源平台只描述输入，不改变 auto_approve、auto_upload 或上传目标。

运行链路回归：

```text
python -m pytest tests/integration/test_source_runtime.py tests/integration/test_recording_metadata.py tests/integration/test_auto_live_startup.py tests/unit/test_recording_controls.py tests/unit/test_danmaku.py
```

## 独立示例与安装

可直接复制的正式示例位于 [plugin/live-source-example](../plugin/live-source-example/README.md)。
目录内含完整清单、入口和设置声明；只连接本地测试服务，不含真实抖音逻辑。
它通过本插件设置提供专用访问头，只向同 origin 的初始播放候选附加测试头，未提供弹幕仍可完整分析。
此检查不覆盖 FFmpeg 跟随的媒体重定向或 HLS 内嵌地址；示例媒体服务及播放列表必须可信。
独立抖音项目可直接使用 [接入交接说明](douyin-plugin-handoff.md)。

源码安装流程：

1. 使用包含来源契约 v1 的宿主源码，在其 Python 环境安装宿主及 Web 依赖：
   `python -m pip install -e ".[web]"`。
2. 把插件目录复制到 `PLUGIN_DIR/<plugin-id>/`，目录名与清单 id 完全一致。
   插件自己的依赖用**运行宿主的同一 Python**执行
   `python -m pip install -r <插件目录>/requirements.txt`，然后 `python -m pip check`。
   宿主不会自动执行插件的安装脚本或 requirements；本地示例只使用宿主已有 HTTPX。
3. 启动 Web，进入“插件”页，刷新、启用插件，再进入设置填写平台凭据。
   设置 API 为 `GET/PATCH /api/plugins/{id}/settings`；写入格式
   `{"values":{"cookie":"平台凭据"}}`，由现有管理员会话鉴权。
   密码空字符串保留原值，null 清空；普通设置值由声明的 Schema 校验。
4. 在直播间页按地址添加，或显式选择平台。登记授权后，分别选择自动录制、分析、渲染、审核和上传。
   停用插件前无需手动删房间；宿主会停止对应来源的活动录制。
5. CLI 沿用同一数据库、工作目录和 `PLUGIN_DIR` 中保存的启用状态。CLI 没有独立插件启用子命令，
   先在 Web 插件中心完成启用和设置，再使用本文 add-room/check/record 命令。

若通过 wheel 安装，公共契约是普通 Python 包，示例和文档位于
`<Python安装前缀>/share/bili-live-cut/{plugin,docs}`；示例不会自动启用或写入 storage。
sdist 保留原有 `plugin/` 和 `docs/` 目录。

### Portable 的依赖边界

Portable 的业务源码来自冻结 Git 提交，发行号相同也可能不包含此新契约。
Launcher 没有把宿主作为包装进 `.venv`，而是给服务子进程设置当前 release 的源码路径。
先用 Launcher 初始化，再停止服务，在 Portable 安装根目录的 PowerShell 读取实际 Runtime 后检查：

```powershell
$blcCurrent = Get-Content -LiteralPath runtime/current.json -Raw | ConvertFrom-Json
$blcSource = Resolve-Path (Join-Path runtime/releases $blcCurrent.release_id)
$env:BLC_SOURCE_DIR = $blcSource.Path
$env:PYTHONPATH = $blcSource.Path
.\.venv\Scripts\python.exe -c "import os; from pathlib import Path; import app.plugins.live_source as api; assert Path(api.__file__).resolve().is_relative_to(Path(os.environ['BLC_SOURCE_DIR']).resolve()); print(api.LIVE_SOURCE_API_VERSION, api.__file__)"
```

输出须为契约版本 `1`，模块路径须位于当前 Runtime；CLI 在同一窗口使用相同源码环境。
关闭此 PowerShell 即结束这两个临时环境变量。使用安装根目录的
`.venv/Scripts/python.exe`，不要使用系统 Python 或独立的 Engine Pack Python。
核对 `--version` 后，插件作者应提供对应 Windows x64 / Python ABI 的 wheels、
包含全部额外依赖版本和 SHA-256 的插件 requirements，并保证兼容宿主锁定的核心版本。
推荐离线增装示例（路径替换为实际插件目录）：

```text
.venv\Scripts\python.exe -m pip install --no-index --find-links storage/plugins/douyin-live/wheels --no-deps --require-hashes -r storage/plugins/douyin-live/requirements.txt
.venv\Scripts\python.exe -m pip check
```

`--no-deps` 要求插件 requirements 已列全额外依赖，不代表允许缺少依赖。
不要降级/升级宿主锁定的 HTTPX/Pydantic 等核心包、替换运行时锁或对 Portable 执行 `pip install -e`。
Launcher 只核对其锁内依赖，不会替插件恢复额外包；环境重建或换安装目录后需重新安装插件依赖。
与核心版本冲突的插件须由插件作者解决约束，不能靠修改宿主运行时锁绕过。

本次只同步了未来 Payload 收录规则，**没有生成包含新接口的 Portable EXE/Full Bundle**。
`0.1.18.4-alpha` 的冻结源码包含本接口，具体 source_commit 以 Portable 的 version.json 和 Payload 清单为准。
发行维护者必须先提交业务源码，再冻结并构建、验证 Portable；构建器拒绝与冻结基线不同的工作区源码。
不能将工作区复制进旧 Payload，也不能把 `0.1.18.3-alpha` 旧包的通过结果当作本功能已打包。

## 完整验收与边界

在独立检出、隔离数据库和临时媒体目录执行；测试不读取真实平台凭据，不迁移用户数据库。
先安装 `.[dev,web]`，确保 FFmpeg/FFprobe 在 PATH：

```text
python -m pytest tests/integration/test_live_source_example.py tests/unit/pipeline/test_event_first_clip_scoring.py --fail-on-skip
python -m pytest --fail-on-skip
python scripts/run_ruff.py check
python scripts/run_ruff.py format
python scripts/check_version_consistency.py
python scripts/release_audit.py --quick
```

Windows 临时目录受权限限制时加 `--basetemp .local/pytest-contract-独立目录名`。
尚未纳入 Git 索引的新 Python 文件也必须单独运行 Ruff；run_ruff 脚本默认只检查已跟踪文件。
项目当前未配置 mypy/Pyright，独立类型检查不适用。

集成测试通过正式 PluginManager 加载磁盘上的示例，使用真实 HTTP 服务、生成的 FLV/HLS、
真实 FFmpeg 分段、SQLModel 入库、持久任务领取和生产阶段提交。
仅替换模型推理及外部 LLM 调用边界；不替换 Recorder、热点检测、队列状态机和渲染。
验证重连换新地址、过期过滤、请求头隔离、重复入队幂等、插件停用收尾、重启恢复，
以及无弹幕时转写、候选、人工审核、出片和文案仍可完成。
联调同时修复晚确认热点补评分后未关联处理任务的问题：保留原分析租约，
候选和任务承接原子提交；证据再次变化时回滚并走原有有界重试，不独立遗留候选。

已有 Bilibili qn、Cookie、匿名弹幕兜底、采样和历史场次回归继续运行；既有高光插件接口仍兼容。
本次未实现抖音算法，也未执行真实抖音实播或真实外部模型质量评估。
