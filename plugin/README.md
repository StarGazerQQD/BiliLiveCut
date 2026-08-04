# BiliLiveCut 插件接口

适用主程序：`V0.1.16.5 Alpha`；当前插件 API 版本仍为 `1`。本次严重正确性修复没有改变插件清单、生命周期或 `HighlightScoringRequest` 契约；插件仍接收完整原始分片上下文，宿主的规则特征和最终 LLM 理由则按音频峰值候选窗口计算。

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
  "settings_page": true,
  "capabilities": []
}
```

- `id`：小写字母开头，只允许小写字母、数字、`_` 和 `-`，最长 64 个字符。
- `api_version`：必须与 `app.plugins.PLUGIN_API_VERSION` 一致；当前为 `1`。
- `entrypoint`：插件目录内的相对 Python 文件与零参数工厂/类，格式为 `file.py:Symbol`。绝对路径、`..`、符号链接入口会被拒绝。
- `settings_page`：是否显示“进入设置”按钮。设置页面由宿主根据插件声明的 Schema 安全渲染。
- `capabilities`：插件提供的可选业务能力。当前支持 `highlight_scorer`；同一时间只允许启用一个高光评分提供者。

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

入口可以导入插件目录中的自有包，例如 `from my_plugin import service`。宿主会在入口执行期间把当前插件目录加入模块搜索路径，并在停用时清理本次加载的插件模块；包名应使用项目专属前缀，避免和其他插件冲突。

## 高光评分能力

高光模型插件在清单中声明 `"capabilities": ["highlight_scorer"]`，并在入口对象上实现同步方法：

```python
from app.plugins import (
    BasePlugin,
    HighlightFeedback,
    HighlightScoringRequest,
    HighlightScoringResult,
)


class Plugin(BasePlugin):
    def score_highlight(self, request: HighlightScoringRequest) -> HighlightScoringResult:
        return HighlightScoringResult(
            requested_mode="shadow",
            effective_mode="shadow",
            shadow_version=1,
            shadow_probability=0.75,
        )

    def record_highlight_feedback(self, feedback: HighlightFeedback) -> None:
        # 此处省略存储实现：相同 sample_id 必须幂等覆盖，
        # label=None 表示删除旧训练标签。
        ...
```

`HighlightScoringRequest` 只包含无 ORM 的只读数据：片段/会话/房间标识、时间边界、转写、词时间戳、弹幕窗口、聚合音频特征、辅助 ASR 特征、规则分和房间级模式覆盖。插件不得自行读取主程序数据库。

`HighlightScoringResult` 支持 `off`、`shadow` 和 `champion`：

- `off`：不执行模型；
- `shadow`：记录模型概率，但不改变主评分；
- `champion`：`champion_probability` 替换规则主评分，再进入原有 LLM 融合和审核阈值；
- 插件抛出异常、返回错误类型或无效概率时，宿主记录回退信息并继续使用规则分。

声明 `highlight_scorer` 的插件还必须实现 `record_highlight_feedback(feedback)`。宿主在人工审核事务提交后，把产生该预测的插件 ID、稳定 `sample_id`、标签、审核来源、Schema 身份和预测时保存的特征快照传回原插件：

- 明确批准返回 `label=1`；
- `rejected` 或 `not_exciting` 返回 `label=0`；
- 保留、素材/边界问题及撤销返回 `label=None`，插件必须删除相同 `sample_id` 的旧训练样本；
- 插件未启用、特征快照不可审计或反馈写入失败时，不回滚已提交的人工审核，宿主会记录隔离错误。

高光评分是同步 Worker 接口，不得在方法中启动未受控后台任务或执行无超时网络请求。训练、模型下载和大规模数据处理应在评分路径之外完成。

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
