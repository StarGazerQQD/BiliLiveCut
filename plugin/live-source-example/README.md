# 独立直播源示例

此目录可原样复制到宿主 `PLUGIN_DIR/live-source-example/`，由正式加载器启用。
`main.py` 只使用 `app.plugins`、`app.plugins.live_source` 和已有 HTTPX 依赖；
不访问数据库，不启动录制器、监控器或第二套任务队列，不提供弹幕。

它是本地 HTTP 服务的适配示例，不是抖音实现，也不是网络直播服务。
测试服务由 `tests/integration/test_live_source_example.py` 启动并销毁，无需真实账号。
启用后可在插件设置页面保存 `api_origin` 和密码字段 `access_key`；未配置时取流会返回认证错误。
密码修改后的后续来源请求读取新值，临时播放地址不会写入插件设置。

## 测试服务约定

来源平台固定为 `example_live`，只接受 `127.0.0.1` 与 `localhost`。
所有请求要求 `X-Example-Key`；只向同一 origin 的初始播放候选附加该测试凭据。
FFmpeg 后续请求可能跟随媒体重定向及 HLS 引用，示例媒体服务和播放列表必须可信。

| 请求 | 返回 JSON |
| --- | --- |
| `GET /resolve?value=...` | `SourceRoom`：平台、稳定字符串 ID、规范地址 |
| `GET /rooms/{百分号编码的ID}/info` | `RoomSnapshot`：live/offline/unknown、可选标题与主播 |
| `GET /rooms/{百分号编码的ID}/streams?intent=best&transport=hls` | 按偏好排序的 `StreamSpec` 数组 |

服务器 HTTP 401/403 对应认证失败，429 对应限流，5xx 和网络失败对应暂时故障。
示例的 HTTPX API 请求拒绝重定向；真实平台的短链解析应在插件内限制跳转域名、次数和请求超时。

## 真实链路验收

在包含此接口的宿主源码根目录、已安装开发依赖且 PATH 有 FFmpeg/FFprobe 的环境运行：

```text
python -m pytest tests/integration/test_live_source_example.py --fail-on-skip
```

测试生成真实音视频，通过加载器读取本目录、持久化设置、登记字符串房间，
用 FFmpeg 实际录制 FLV/HLS、重连刷新地址、停止时收尾并登记持久任务。
随后执行正式任务领取、热点检测、音频标准化、转写结果入库、候选评分、人工审核门禁、
FFmpeg 渲染与文案生成；仅 ASR 模型推理和外部 LLM 调用由离线替身提供。
还验证录制中停用、来源重启恢复、设置隐藏回显、跨平台 Cookie 隔离和任务幂等。

完整字段、生命周期、安装和 Portable 限制见 [直播源插件文档](../../docs/live-source-plugins.md)。
