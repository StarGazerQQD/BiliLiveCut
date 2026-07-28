# BiliLiveCut 插件接口

主程序从 `PLUGIN_DIR`（默认 `./storage/plugins`）的直属子目录读取插件。每个插件目录必须包含 `plugin.json`；扫描阶段只解析清单，不导入 Python。只有管理员在控制台“插件”页显式开启后，宿主才会导入入口并调用生命周期钩子。

> 插件与主程序运行在同一 Python 进程，拥有当前用户的文件、网络和进程权限。BiliLiveCut 不把插件当作安全沙箱，请只安装和启用已审查、可信来源的插件。密码类型设置会隐藏回显，但当前保存在本地数据库中，并非加密保险箱。

## 目录与清单

把插件放入以下结构，目录名必须与清单 `id` 完全一致：

```text
storage/plugins/
└─ my-plugin/
   ├─ plugin.json
   └─ main.py
```

`plugin.json`：

```json
{
  "id": "my-plugin",
  "name": "我的插件",
  "version": "1.0.0",
  "api_version": "1",
  "entrypoint": "main.py:Plugin",
  "description": "插件说明",
  "settings_page": true
}
```

- `id`：小写字母开头，只允许小写字母、数字、`_` 和 `-`，最长 64 个字符。
- `api_version`：必须与 `app.plugins.PLUGIN_API_VERSION` 一致；当前为 `1`。
- `entrypoint`：插件目录内的相对 Python 文件与零参数工厂/类，格式为 `file.py:Symbol`。绝对路径、`..`、符号链接入口会被拒绝。
- `settings_page`：是否显示“进入设置”按钮。设置页面由宿主根据插件声明的 Schema 安全渲染。

完整 JSON Schema 见 [`manifest.schema.json`](manifest.schema.json)，可运行示例见 [`example/`](example/)。

## Python 契约

入口工厂必须返回满足 `BiliLiveCutPlugin` 的对象。推荐继承 `BasePlugin`：

```python
from app.plugins import BasePlugin, PluginContext, PluginSetting


class Plugin(BasePlugin):
    settings_schema = (
        PluginSetting(
            key="endpoint",
            label="服务地址",
            kind="text",
            default="http://127.0.0.1:9000",
            required=True,
        ),
        PluginSetting(key="enabled_notice", label="发送通知", kind="boolean", default=True),
    )

    def on_enable(self, context: PluginContext) -> None:
        self.context = context

    def on_disable(self) -> None:
        pass
```

生命周期钩子可以是同步函数或 `async def`：

- `on_enable(context)`：显式启用或服务启动恢复开关时调用。抛出异常会使本次启用失败并在插件中心显示错误。
- `on_disable()`：显式停用、插件清单变化/移除或服务关闭时调用。插件应在这里停止自建任务并释放资源。
- `settings_schema`：`PluginSetting` 序列。支持 `text`、`number`、`boolean`、`select`、`password`。

`PluginContext` 只暴露当前插件目录和带命名空间的持久化设置：

```python
value = context.get_setting("endpoint", "http://127.0.0.1:9000")
context.set_setting("last_cursor", "abc123")
```

插件不得依赖 `app.plugins.manager` 的私有实现。跨 API 主版本不保证兼容；发布新主版本时插件应更新清单并重新验证。

## 安装、启停与设置

1. 停止服务，把完整插件目录复制到 `PLUGIN_DIR`；也可在服务运行时复制后点击“重新扫描”。
2. 打开控制台 → “配置” → “插件”。确认名称、版本、来源和错误信息。
3. 打开启用开关。只有此时入口代码才会执行。
4. 点击“进入设置”，填写插件自己的设置并保存。
5. 更新插件前先停用；替换文件后重新扫描并再次启用。

HTTP 管理接口沿用主程序管理员鉴权和 CSRF 规则：

- `GET /api/plugins`
- `POST /api/plugins/refresh`
- `PATCH /api/plugins/{id}`，请求体 `{"enabled": true}`
- `GET /api/plugins/{id}/settings`
- `PATCH /api/plugins/{id}/settings`，请求体 `{"values": {...}}`

## 兼容性与故障边界

- 无效清单、目录名不匹配、API 版本不兼容不会被加载，并会作为扫描错误展示。
- 停用插件会移除当前入口模块并调用清理钩子，但 Python 无法撤销插件已经产生的外部副作用。
- 主程序仅持久化已声明字段；未知字段和错误类型会被拒绝。`password` 留空保存表示保留原值。
- 插件内部后台任务必须自行实现取消、超时、幂等和异常日志，且不得阻塞事件循环。
