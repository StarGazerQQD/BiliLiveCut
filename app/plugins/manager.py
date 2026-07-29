"""本地插件发现、启停、设置与生命周期管理。"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import cast

from loguru import logger
from pydantic import ValidationError

from app.core import config
from app.core.settings_store import get_bool, get_setting, set_bool, set_setting
from app.plugins.contracts import (
    PLUGIN_API_VERSION,
    BiliLiveCutPlugin,
    PluginCapability,
    PluginContext,
    PluginManifest,
    PluginSetting,
    PluginSettingValue,
)
from app.plugins.highlight import (
    HighlightDispatch,
    HighlightFeedback,
    HighlightFeedbackDispatch,
    HighlightScoringPlugin,
    HighlightScoringRequest,
    HighlightScoringResult,
)

_MAX_MANIFEST_BYTES = 64 * 1024
_SETTING_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class PluginError(RuntimeError):
    """插件操作失败的基类。"""


class PluginNotFoundError(PluginError):
    """请求的插件不存在。"""


class PluginStateError(PluginError):
    """插件当前状态不允许执行操作。"""


class PluginValidationError(PluginError):
    """插件清单或设置值无效。"""


@dataclass(slots=True)
class _PluginRecord:
    manifest: PluginManifest
    directory: Path
    enabled: bool = False
    instance: BiliLiveCutPlugin | None = None
    schema: tuple[PluginSetting, ...] = ()
    module_name: str | None = None
    module_names: tuple[str, ...] = ()
    error: str | None = None

    @property
    def loaded(self) -> bool:
        return self.instance is not None


@dataclass(slots=True)
class _Discovery:
    records: dict[str, _PluginRecord] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)


class PluginManager:
    """管理一个插件目录中的清单、实例和设置。"""

    def __init__(self, root: Path | None = None) -> None:
        """创建管理器；省略 ``root`` 时使用运行时配置。"""
        self._configured_root = root
        self._records: dict[str, _PluginRecord] = {}
        self._scan_errors: list[dict[str, str]] = []
        self._started = False
        self._lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        """返回当前插件目录的绝对路径。"""
        configured = self._configured_root if self._configured_root is not None else Path(config.settings.plugin_dir)
        return configured.expanduser().resolve()

    async def start(self) -> None:
        """创建目录、发现插件并恢复持久化启用状态。"""
        self.root.mkdir(parents=True, exist_ok=True)
        self._started = True
        await self.refresh()

    async def stop(self) -> None:
        """按当前加载状态停用所有插件但保留持久化开关。"""
        async with self._lock:
            for record in list(self._records.values()):
                if record.loaded:
                    await self._deactivate(record)
            self._started = False

    async def refresh(self) -> list[dict[str, object]]:
        """重新扫描目录，并按已保存状态装载新出现的插件。"""
        async with self._lock:
            discovery = self._discover()
            previous = self._records

            for plugin_id, record in previous.items():
                replacement = discovery.records.get(plugin_id)
                unchanged = replacement is not None and replacement.manifest == record.manifest
                if record.loaded and not unchanged:
                    await self._deactivate(record)

            merged: dict[str, _PluginRecord] = {}
            for plugin_id, record in discovery.records.items():
                old = previous.get(plugin_id)
                if old is not None and old.manifest == record.manifest and old.directory == record.directory:
                    old.enabled = self._enabled_setting(plugin_id)
                    merged[plugin_id] = old
                else:
                    record.enabled = self._enabled_setting(plugin_id)
                    merged[plugin_id] = record

            self._records = merged
            self._scan_errors = discovery.errors
            if self._started:
                for record in self._records.values():
                    if record.enabled and not record.loaded:
                        try:
                            await self._activate(record)
                        except PluginError as exc:
                            logger.error("插件 {} 自动启用失败: {}", record.manifest.id, exc)
            return self._serialize_plugins()

    async def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, object]:
        """显式启用或停用插件，并持久化用户选择。"""
        await self.refresh()
        async with self._lock:
            record = self._require_record(plugin_id)
            if enabled:
                if not record.loaded:
                    try:
                        await self._activate(record)
                    except PluginError:
                        set_bool(self._enabled_key(plugin_id), False)
                        record.enabled = False
                        raise
                set_bool(self._enabled_key(plugin_id), True)
                record.enabled = True
            else:
                if record.loaded:
                    await self._deactivate(record)
                set_bool(self._enabled_key(plugin_id), False)
                record.enabled = False
            return self._serialize_record(record)

    def list_payload(self) -> dict[str, object]:
        """返回 UI 可消费的插件与扫描错误。"""
        return {
            "plugins": self._serialize_plugins(),
            "scan_errors": list(self._scan_errors),
        }

    def descriptor(self, plugin_id: str) -> dict[str, object]:
        """返回单个插件的公开描述。"""
        return self._serialize_record(self._require_record(plugin_id))

    def settings_payload(self, plugin_id: str) -> dict[str, object]:
        """返回已启用插件的设置字段与当前值。"""
        record = self._require_loaded(plugin_id)
        if not record.manifest.settings_page:
            raise PluginStateError("该插件未声明设置页面")
        fields: list[dict[str, object]] = []
        for setting_field in record.schema:
            stored = self._read_setting(plugin_id, setting_field.key, setting_field.default)
            item = setting_field.model_dump(mode="json")
            item["configured"] = stored not in (None, "")
            item["value"] = "" if setting_field.kind == "password" else stored
            fields.append(item)
        return {"plugin": self._serialize_record(record), "fields": fields}

    def update_settings(self, plugin_id: str, values: dict[str, object]) -> dict[str, object]:
        """校验并持久化已启用插件的设置。"""
        record = self._require_loaded(plugin_id)
        schema = {item.key: item for item in record.schema}
        unknown = sorted(set(values) - set(schema))
        if unknown:
            raise PluginValidationError(f"未知设置项: {', '.join(unknown)}")
        for key, raw_value in values.items():
            setting_field = schema[key]
            if setting_field.kind == "password" and raw_value == "":
                continue
            value = self._validate_setting_value(setting_field, raw_value)
            self._write_setting(plugin_id, key, value)
        return self.settings_payload(plugin_id)

    def score_highlight(self, request: HighlightScoringRequest) -> HighlightDispatch | None:
        """调用唯一已启用的高光评分插件，并隔离第三方异常。"""
        providers = [
            record
            for record in self._records.values()
            if record.loaded and "highlight_scorer" in record.manifest.capabilities
        ]
        if not providers:
            return None
        record = providers[0]
        instance = record.instance
        if instance is None or not isinstance(instance, HighlightScoringPlugin):
            return HighlightDispatch(plugin_id=record.manifest.id, error="已启用插件不满足高光评分契约")
        try:
            prediction = instance.score_highlight(request)
            if not isinstance(prediction, HighlightScoringResult):
                raise TypeError("score_highlight 必须返回 HighlightScoringResult")
            return HighlightDispatch(plugin_id=record.manifest.id, prediction=prediction)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.opt(exception=exc).warning(
                "高光评分插件失败，已回退规则评分: plugin={} segment={}",
                record.manifest.id,
                request.segment_id,
            )
            return HighlightDispatch(plugin_id=record.manifest.id, error=error)

    def record_highlight_feedback(self, feedback: HighlightFeedback) -> HighlightFeedbackDispatch:
        """把人工审核反馈投递给产生该预测的已启用插件，并隔离异常。"""
        record = self._records.get(feedback.plugin_id)
        if record is None or not record.loaded or "highlight_scorer" not in record.manifest.capabilities:
            return HighlightFeedbackDispatch(
                plugin_id=feedback.plugin_id,
                error="产生该预测的高光评分插件当前未启用",
            )
        instance = record.instance
        if instance is None or not isinstance(instance, HighlightScoringPlugin):
            return HighlightFeedbackDispatch(
                plugin_id=feedback.plugin_id,
                error="已启用插件不满足高光评分反馈契约",
            )
        try:
            instance.record_highlight_feedback(feedback)
            return HighlightFeedbackDispatch(plugin_id=feedback.plugin_id, delivered=True)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.opt(exception=exc).warning(
                "高光评分插件反馈写入失败: plugin={} candidate={}",
                feedback.plugin_id,
                feedback.candidate_id,
            )
            return HighlightFeedbackDispatch(plugin_id=feedback.plugin_id, error=error)

    def has_capability(self, capability: PluginCapability) -> bool:
        """返回当前是否有已加载的指定能力提供者。"""
        return any(record.loaded and capability in record.manifest.capabilities for record in self._records.values())

    def _discover(self) -> _Discovery:
        result = _Discovery()
        root = self.root
        if not root.exists():
            return result
        for directory in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            try:
                if directory.is_symlink():
                    raise PluginValidationError("插件目录不能是符号链接")
                manifest_path = directory / "plugin.json"
                if not manifest_path.is_file() or manifest_path.is_symlink():
                    continue
                if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
                    raise PluginValidationError("plugin.json 超过 64 KiB")
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = PluginManifest.model_validate(raw)
                if manifest.id != directory.name:
                    raise PluginValidationError("清单 id 必须与插件目录名一致")
                if manifest.api_version != PLUGIN_API_VERSION:
                    raise PluginValidationError(
                        f"不兼容的 api_version={manifest.api_version}，宿主仅支持 {PLUGIN_API_VERSION}"
                    )
                result.records[manifest.id] = _PluginRecord(manifest=manifest, directory=directory.resolve())
            except (OSError, json.JSONDecodeError, ValidationError, PluginValidationError) as exc:
                result.errors.append({"directory": directory.name, "error": str(exc)})
        return result

    async def _activate(self, record: _PluginRecord) -> None:
        module_name: str | None = None
        module_names: tuple[str, ...] = ()
        instance: BiliLiveCutPlugin | None = None
        try:
            instance, module_name, module_names = self._load_instance(record)
            self._validate_capabilities(record, instance)
            schema = self._validate_schema(instance.settings_schema)
            outcome = instance.on_enable(self._context(record))
            if inspect.isawaitable(outcome):
                await outcome
            record.instance = instance
            record.schema = schema
            record.module_name = module_name
            record.module_names = module_names
            record.error = None
            logger.info("插件已启用: {} {}", record.manifest.id, record.manifest.version)
        except Exception as exc:
            if instance is not None:
                try:
                    cleanup = instance.on_disable()
                    if inspect.isawaitable(cleanup):
                        await cleanup
                except Exception as cleanup_exc:
                    logger.error("插件 {} 启用回滚失败: {}", record.manifest.id, cleanup_exc)
            record.instance = None
            record.schema = ()
            record.error = str(exc)
            for imported_name in module_names:
                sys.modules.pop(imported_name, None)
            record.module_name = None
            record.module_names = ()
            if isinstance(exc, PluginError):
                raise
            raise PluginStateError(f"插件 {record.manifest.id} 启用失败: {exc}") from exc

    async def _deactivate(self, record: _PluginRecord) -> None:
        instance = record.instance
        record.instance = None
        record.schema = ()
        try:
            if instance is not None:
                outcome = instance.on_disable()
                if inspect.isawaitable(outcome):
                    await outcome
        except Exception as exc:
            record.error = f"停用钩子失败: {exc}"
            logger.error("插件 {} 停用钩子失败: {}", record.manifest.id, exc)
        finally:
            for imported_name in record.module_names:
                sys.modules.pop(imported_name, None)
            record.module_name = None
            record.module_names = ()

    def _load_instance(self, record: _PluginRecord) -> tuple[BiliLiveCutPlugin, str, tuple[str, ...]]:
        relative_path, _, symbol = record.manifest.entrypoint.partition(":")
        root = record.directory.resolve()
        candidate = root / relative_path
        if candidate.is_symlink():
            raise PluginValidationError("插件入口不能是符号链接")
        module_path = candidate.resolve()
        try:
            module_path.relative_to(root)
        except ValueError as exc:
            raise PluginValidationError("插件入口越出插件目录") from exc
        if not module_path.is_file() or module_path.is_symlink():
            raise PluginValidationError(f"插件入口不存在或为符号链接: {relative_path}")

        module_name = f"_bililivecut_plugin_{record.manifest.id.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise PluginValidationError("无法创建插件模块加载器")
        module = importlib.util.module_from_spec(spec)
        previous_modules = set(sys.modules)
        sys.modules[module_name] = module
        root_entry = str(root)
        sys.path.insert(0, root_entry)
        try:
            self._execute_module(spec.loader, module)
            factory = getattr(module, symbol)
            instance = factory()
        except Exception:
            for imported_name in self._module_names_from_root(root, previous_modules):
                sys.modules.pop(imported_name, None)
            raise
        finally:
            if sys.path and sys.path[0] == root_entry:
                sys.path.pop(0)
            else:
                try:
                    sys.path.remove(root_entry)
                except ValueError:
                    pass
        module_names = self._module_names_from_root(root, previous_modules)
        if not isinstance(instance, BiliLiveCutPlugin):
            for imported_name in module_names:
                sys.modules.pop(imported_name, None)
            raise PluginValidationError("入口对象不满足 BiliLiveCutPlugin 契约")
        return cast(BiliLiveCutPlugin, instance), module_name, module_names

    @staticmethod
    def _module_names_from_root(root: Path, previous_modules: set[str]) -> tuple[str, ...]:
        """返回本次入口加载中新导入且来源位于插件目录的模块名。"""
        names: list[str] = []
        for name in set(sys.modules).difference(previous_modules):
            module = sys.modules.get(name)
            raw_path = getattr(module, "__file__", None)
            if not isinstance(raw_path, str):
                continue
            try:
                Path(raw_path).resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            names.append(name)
        return tuple(sorted(names))

    def _validate_capabilities(self, record: _PluginRecord, instance: BiliLiveCutPlugin) -> None:
        """校验声明能力的接口形状及单提供者约束。"""
        if "highlight_scorer" not in record.manifest.capabilities:
            return
        if not isinstance(instance, HighlightScoringPlugin):
            raise PluginValidationError(
                "声明 highlight_scorer 的插件必须实现 score_highlight(request) 和 record_highlight_feedback(feedback)"
            )
        conflict = next(
            (
                other.manifest.id
                for other in self._records.values()
                if other is not record and other.loaded and "highlight_scorer" in other.manifest.capabilities
            ),
            None,
        )
        if conflict is not None:
            raise PluginStateError(f"高光评分提供者已启用: {conflict}")

    @staticmethod
    def _execute_module(loader: object, module: ModuleType) -> None:
        exec_module = getattr(loader, "exec_module", None)
        if not callable(exec_module):
            raise PluginValidationError("插件模块加载器不支持 exec_module")
        exec_module(module)

    @staticmethod
    def _validate_schema(raw_schema: object) -> tuple[PluginSetting, ...]:
        try:
            schema = tuple(raw_schema)  # type: ignore[arg-type]
        except TypeError as exc:
            raise PluginValidationError("settings_schema 必须是 PluginSetting 序列") from exc
        if not all(isinstance(item, PluginSetting) for item in schema):
            raise PluginValidationError("settings_schema 只能包含 PluginSetting")
        keys = [item.key for item in schema]
        if len(keys) != len(set(keys)):
            raise PluginValidationError("settings_schema 中存在重复 key")
        return schema

    def _context(self, record: _PluginRecord) -> PluginContext:
        return PluginContext(
            plugin_id=record.manifest.id,
            plugin_dir=record.directory,
            _get_setting=lambda key, default: self._read_setting(record.manifest.id, key, default),
            _set_setting=lambda key, value: self._write_setting(record.manifest.id, key, value),
        )

    @staticmethod
    def _validate_setting_value(setting_field: PluginSetting, raw_value: object) -> PluginSettingValue:
        if raw_value is None:
            if setting_field.required:
                raise PluginValidationError(f"{setting_field.key} 不能为空")
            return None
        kind = setting_field.kind
        if kind == "boolean":
            if not isinstance(raw_value, bool):
                raise PluginValidationError(f"{setting_field.key} 必须是布尔值")
            value: PluginSettingValue = raw_value
        elif kind == "number":
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                raise PluginValidationError(f"{setting_field.key} 必须是数值")
            numeric = float(raw_value)
            if setting_field.minimum is not None and numeric < setting_field.minimum:
                raise PluginValidationError(f"{setting_field.key} 不能小于 {setting_field.minimum}")
            if setting_field.maximum is not None and numeric > setting_field.maximum:
                raise PluginValidationError(f"{setting_field.key} 不能大于 {setting_field.maximum}")
            value = numeric
        else:
            if not isinstance(raw_value, str):
                raise PluginValidationError(f"{setting_field.key} 必须是字符串")
            if setting_field.required and not raw_value.strip():
                raise PluginValidationError(f"{setting_field.key} 不能为空")
            if kind == "select" and raw_value not in setting_field.choices:
                raise PluginValidationError(f"{setting_field.key} 不在允许选项中")
            value = raw_value
        return value

    @staticmethod
    def _enabled_key(plugin_id: str) -> str:
        return f"plugin.{plugin_id}.enabled"

    @staticmethod
    def _setting_key(plugin_id: str, key: str) -> str:
        if not _SETTING_KEY_RE.fullmatch(key):
            raise PluginValidationError("插件设置 key 格式无效")
        return f"plugin.{plugin_id}.setting.{key}"

    def _enabled_setting(self, plugin_id: str) -> bool:
        return get_bool(self._enabled_key(plugin_id))

    def _read_setting(
        self,
        plugin_id: str,
        key: str,
        default: PluginSettingValue,
    ) -> PluginSettingValue:
        raw = get_setting(self._setting_key(plugin_id, key), "")
        if raw == "":
            return default
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return default
        return value if isinstance(value, (str, int, float, bool)) or value is None else default

    def _write_setting(self, plugin_id: str, key: str, value: PluginSettingValue) -> None:
        set_setting(self._setting_key(plugin_id, key), json.dumps(value, ensure_ascii=False))

    def _require_record(self, plugin_id: str) -> _PluginRecord:
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginNotFoundError(f"插件不存在: {plugin_id}")
        return record

    def _require_loaded(self, plugin_id: str) -> _PluginRecord:
        record = self._require_record(plugin_id)
        if not record.enabled or not record.loaded:
            raise PluginStateError("请先启用插件")
        return record

    def _serialize_plugins(self) -> list[dict[str, object]]:
        return [self._serialize_record(record) for record in self._records.values()]

    @staticmethod
    def _serialize_record(record: _PluginRecord) -> dict[str, object]:
        manifest = record.manifest
        return {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "api_version": manifest.api_version,
            "capabilities": list(manifest.capabilities),
            "description": manifest.description,
            "enabled": record.enabled,
            "loaded": record.loaded,
            "error": record.error,
            "has_settings": manifest.settings_page,
            "settings_url": f"/plugins/{manifest.id}/settings" if manifest.settings_page else None,
        }


plugin_manager = PluginManager()
"""Web 应用使用的进程级插件管理器。"""
