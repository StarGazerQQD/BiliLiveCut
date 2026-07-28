"""BiliLiveCut 最小插件示例。"""

from __future__ import annotations

from app.plugins import BasePlugin, PluginContext, PluginSetting


class Plugin(BasePlugin):
    """演示宿主生命周期和持久化设置。"""

    settings_schema = (
        PluginSetting(
            key="greeting",
            label="问候语",
            description="插件可通过 PluginContext 读取该值。",
            default="你好，BiliLiveCut",
            required=True,
        ),
        PluginSetting(
            key="repeat",
            label="重复次数",
            kind="number",
            default=1,
            minimum=1,
            maximum=5,
        ),
        PluginSetting(
            key="show_notice",
            label="显示通知",
            kind="boolean",
            default=True,
        ),
    )

    def __init__(self) -> None:
        self.context: PluginContext | None = None

    def on_enable(self, context: PluginContext) -> None:
        """保存宿主上下文，供插件业务代码读取设置。"""
        self.context = context

    def on_disable(self) -> None:
        """释放对宿主上下文的引用。"""
        self.context = None
