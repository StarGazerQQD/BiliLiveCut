"""BiliLiveCut 插件接口与运行时管理。"""

from app.plugins.contracts import (
    PLUGIN_API_VERSION,
    BasePlugin,
    BiliLiveCutPlugin,
    PluginCapability,
    PluginContext,
    PluginManifest,
    PluginSetting,
    PluginSettingValue,
)
from app.plugins.highlight import (
    HighlightAudio,
    HighlightDanmaku,
    HighlightDispatch,
    HighlightFeedback,
    HighlightFeedbackDispatch,
    HighlightLabel,
    HighlightScoringPlugin,
    HighlightScoringRequest,
    HighlightScoringResult,
    HighlightWord,
    RoomScoringMode,
    ScoringMode,
)

__all__ = [
    "PLUGIN_API_VERSION",
    "BasePlugin",
    "BiliLiveCutPlugin",
    "HighlightAudio",
    "HighlightDanmaku",
    "HighlightDispatch",
    "HighlightFeedback",
    "HighlightFeedbackDispatch",
    "HighlightLabel",
    "HighlightScoringPlugin",
    "HighlightScoringRequest",
    "HighlightScoringResult",
    "HighlightWord",
    "PluginCapability",
    "PluginContext",
    "PluginManifest",
    "PluginSetting",
    "PluginSettingValue",
    "RoomScoringMode",
    "ScoringMode",
]
