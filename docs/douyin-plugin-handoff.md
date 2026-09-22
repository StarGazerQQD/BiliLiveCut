# 交给独立抖音插件项目的接入说明

请在独立 Git 项目实现 BiliLiveCut 抖音直播源插件。宿主必须提供
`app.plugins.live_source.LIVE_SOURCE_API_VERSION == "1"`；最低正式宿主版本为 `0.1.18.4-alpha`。
加载时仍检查契约版本，旧宿主会拒绝新能力清单。
完整契约见 [live-source-plugins.md](live-source-plugins.md)，可加载示例见
[live-source-example](../plugin/live-source-example/README.md)。

只使用以下公共导入：

```python
from app.plugins import BasePlugin, PluginContext, PluginSetting
from app.plugins.live_source import (
    LIVE_SOURCE_API_VERSION, LiveSource, SourceDescriptor, SourceRoom,
    RoomSnapshot, LiveStatus, StreamPreference, StreamSpec,
    SourceError, SourceTemporaryError, SourceRateLimited,
    SourceAuthenticationError, SourceInvalidInput, SourceUnavailable,
    DanmakuSource, DanmakuEvent, DanmakuStatus, DanmakuSink, DanmakuStateSink,
)
```

插件入口继承 `BasePlugin`，声明 `settings_schema`；`on_enable(context)` 创建来源，
调用 `context.register_live_source(source)`。宿主接管来源关闭，不导入宿主 ORM、注册器、
Recorder、监控器和调度器，不 monkey patch 主程序。

实现 `descriptor = SourceDescriptor(platform="douyin", name="抖音", domains=(...))`，
其中 domains 必须是经插件项目确认的精确主机名，并包含支持的短链域名。
实现四个可取消的异步方法：

```python
async def resolve_room(self, value: str) -> SourceRoom: ...
async def get_room_info(self, room: SourceRoom) -> RoomSnapshot: ...
async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]: ...
async def aclose(self) -> None: ...
```

稳定字符串 `source_id` 是平台房间身份，不能使用变化的直播场次 ID、伪造整数或宿主主键。
`canonical_url` 必须是无凭据、无查询参数的规范 HTTP(S) 地址。
状态未知返回 `LiveStatus.UNKNOWN`，网络故障抛类型化错误，不得伪造下播。
清晰度使用平台字符串；候选按 preference 排序，每次调用刷新临时播放 URL。
必要请求头放进 `StreamSpec.headers`，只允许 HTTP(S) 输入，没有自定义 FFmpeg 参数入口。
Cookie、token 和签名 URL 不进入日志、公开错误、普通持久字段。

清单保存为 `storage/plugins/douyin-live/plugin.json`，目录名与 id 必须一致：

```json
{
  "id": "douyin-live",
  "name": "抖音直播源",
  "version": "1.0.0",
  "api_version": "1",
  "live_source_api_version": "1",
  "entrypoint": "main.py:Plugin",
  "settings_page": true,
  "capabilities": ["live_source"]
}
```

平台凭据声明为 `PluginSetting(key="cookie", label="抖音 Cookie", kind="password")`，
通过 `context.get_setting("cookie", "")` 读取。密码留空保存表示保留旧值，传 JSON null 可清空；
不要读取 Bilibili Cookie 或要求用户修改宿主私有配置。来源应在每次请求读取设置，
或明确说明改设置后需停用再启用。初始化应允许尚未填写凭据，以便先启用后进入设置页面。

HTTP 客户端由来源拥有，`aclose()` 幂等释放。同步 SDK 必须隔离阻塞 I/O 并配置有限超时；
取消应等已开始的线程工作结束。宿主负责请求预算、限流退避、录制重连及停用收尾。
可选弹幕首版可完全不实现；缺失弹幕不会阻止转写与分析。
若实现 `collect_danmaku(room, emit, state)`，连接成功才上报 AVAILABLE，发 UTC 普通文字事件，
取消返回前关闭连接。不要未经转换发送礼物金额、人气或热度。

平台解析、签名和专有依赖全部留在本插件项目。源码宿主：用运行宿主的同一 Python 执行
`python -m pip install -r <插件目录>/requirements.txt`，再 `python -m pip check`。
Portable：先确认安装的是包含契约 v1 的新宿主；使用该安装根目录
`.venv/Scripts/python.exe`，按主文档安装与其 Python ABI 和锁定核心版本兼容的插件 wheels。
不要使用系统 Python、安装第二份 BiliLiveCut、替换核心依赖锁或把插件混入 Engine Pack。
`0.1.18.3-alpha` 及更早的 Portable 不含此接口，必须升级到包含契约 v1 的宿主后安装插件。

Web 插件页刷新、启用、填写设置后，在直播间页按 URL 自动识别或显式选择抖音。
CLI 共用已保存的启用状态和设置，使用相同数据库与 PLUGIN_DIR：

```text
python -m app.cli add-room <房间ID> --platform douyin --authorize
python -m app.cli check <房间ID> --platform douyin
python -m app.cli record <宿主房间主键> --pipeline
```

宿主隔离契约验收命令：

```text
python -m pytest tests/unit/test_live_sources.py tests/integration/test_source_rooms.py tests/integration/test_source_runtime.py tests/integration/test_live_source_example.py --fail-on-skip
```

独立插件还需以自己的离线 HTTP/SDK 夹具覆盖短链、稳定 ID、未知/下播区分、限流、凭据失效、
过期地址刷新、取消、客户端关闭和私密日志。使用正式加载器验证安装/启停；不要替换宿主核心链路。
真实抖音实播验证应单独记录日期、插件版本、传输类型和脱敏结果；宿主的本地契约测试不代表该验证已完成。
