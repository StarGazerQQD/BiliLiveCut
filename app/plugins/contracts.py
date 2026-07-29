"""BiliLiveCut 插件公共契约。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypeAlias, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

PLUGIN_API_VERSION = "1"
"""当前宿主支持的插件 API 主版本。"""

PluginSettingValue: TypeAlias = str | float | bool | None
PluginSettingKind: TypeAlias = Literal["text", "number", "boolean", "select", "password"]
PluginCapability: TypeAlias = Literal["highlight_scorer"]


class PluginManifest(BaseModel):
    """磁盘 ``plugin.json`` 的稳定元数据格式。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    api_version: str = Field(default=PLUGIN_API_VERSION, min_length=1, max_length=20)
    entrypoint: str = Field(min_length=3, max_length=180)
    description: str = Field(default="", max_length=500)
    settings_page: bool = True
    capabilities: tuple[PluginCapability, ...] = ()

    @model_validator(mode="after")
    def validate_entrypoint(self) -> PluginManifest:
        """拒绝绝对路径、父目录穿越和非 Python 入口。"""
        module_path, separator, symbol = self.entrypoint.partition(":")
        relative = Path(module_path)
        if not separator or not symbol.isidentifier():
            raise ValueError("entrypoint 必须使用 relative_file.py:Symbol 格式")
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".py":
            raise ValueError("entrypoint 必须是插件目录内的相对 .py 文件")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("capabilities 不能重复")
        return self


class PluginSetting(BaseModel):
    """由插件声明、由宿主渲染和持久化的设置字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    kind: PluginSettingKind = "text"
    description: str = Field(default="", max_length=500)
    default: PluginSettingValue = None
    required: bool = False
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> PluginSetting:
        """校验字段类型特有的约束。"""
        if self.kind == "select" and not self.choices:
            raise ValueError("select 设置必须声明 choices")
        if self.kind != "select" and self.choices:
            raise ValueError("只有 select 设置可以声明 choices")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum 不能大于 maximum")
        if self.default is not None:
            if self.kind == "boolean" and not isinstance(self.default, bool):
                raise ValueError("boolean 设置的 default 必须是布尔值")
            if self.kind == "number" and (isinstance(self.default, bool) or not isinstance(self.default, (int, float))):
                raise ValueError("number 设置的 default 必须是数值")
            if self.kind in {"text", "select", "password"} and not isinstance(self.default, str):
                raise ValueError(f"{self.kind} 设置的 default 必须是字符串")
            if self.kind == "select" and self.default not in self.choices:
                raise ValueError("select 设置的 default 必须位于 choices 中")
            if self.kind == "number":
                numeric_default = float(self.default)
                if self.minimum is not None and numeric_default < self.minimum:
                    raise ValueError("number 设置的 default 不能小于 minimum")
                if self.maximum is not None and numeric_default > self.maximum:
                    raise ValueError("number 设置的 default 不能大于 maximum")
        return self


@dataclass(frozen=True, slots=True)
class PluginContext:
    """宿主传给已启用插件的受控上下文。"""

    plugin_id: str
    plugin_dir: Path
    _get_setting: Callable[[str, PluginSettingValue], PluginSettingValue]
    _set_setting: Callable[[str, PluginSettingValue], None]

    def get_setting(self, key: str, default: PluginSettingValue = None) -> PluginSettingValue:
        """读取当前插件命名空间内的设置。"""
        return self._get_setting(key, default)

    def set_setting(self, key: str, value: PluginSettingValue) -> None:
        """写入当前插件命名空间内的设置。"""
        self._set_setting(key, value)


@runtime_checkable
class BiliLiveCutPlugin(Protocol):
    """插件入口对象必须满足的最小接口。"""

    @property
    def settings_schema(self) -> Sequence[PluginSetting]:
        """返回由宿主渲染的设置字段。"""
        ...

    def on_enable(self, context: PluginContext) -> None | Awaitable[None]:
        """插件启用时执行初始化。"""
        ...

    def on_disable(self) -> None | Awaitable[None]:
        """插件停用或宿主关闭时释放资源。"""
        ...


class BasePlugin:
    """提供空生命周期钩子的插件基类。"""

    settings_schema: Sequence[PluginSetting] = ()

    def on_enable(self, context: PluginContext) -> None:
        """默认启用钩子不执行操作。"""

    def on_disable(self) -> None:
        """默认停用钩子不执行操作。"""
